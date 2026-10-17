#!/usr/bin/env python3
"""Chaos scenario orchestrator — `make chaos SCENARIO=<name>`.

Boots an isolated stack and drives one fault scenario through it, then asserts
the outcome:

    controller (temp DB)  ──telemetry──>  node_sim (impersonated node)
          ▲                                     │
          │ GET /v1/telemetry/recent            │
     operator loop  ──LXD REST command──>  lxd_stub (records every call)

Verdicts:
  * injection: the emitted series crosses the scenario's frozen threshold rule
  * remediation: the LXD endpoint received a real command for the target node
    (remediation scenarios) or received nothing (healthy false-positive guard)

The operator is the bundled reference probe by default; point
`OPERATOR_CMD`/`--operator-cmd` at the production loop (t_375d9df4) to validate
it with the exact same evidence. See chaos/README.md.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # allow `python3 chaos/run.py`

from chaos import common, config  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
CONTROLLER_DIR = REPO_ROOT / "controller"
MUTATION_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# --------------------------------------------------------------------------- #
# process helpers
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _popen(argv, log_path: pathlib.Path, env: dict | None = None,
           cwd: pathlib.Path | None = None) -> subprocess.Popen:
    fh = open(log_path, "wb")
    return subprocess.Popen(
        argv, stdout=fh, stderr=subprocess.STDOUT, env=env,
        start_new_session=True, cwd=str(cwd or REPO_ROOT),
    )


def _terminate(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def _read_commands(log_path: pathlib.Path) -> list[dict]:
    cmds = []
    for entry in common.read_jsonl(log_path):
        if entry.get("method") in MUTATION_METHODS and str(entry.get("path", "")).startswith("/1.0"):
            cmds.append(entry)
    return cmds


def _matches_target(cmd: dict, vm_id: str | None) -> bool:
    if not vm_id:
        return True
    return cmd.get("instance") == vm_id or vm_id in str(cmd.get("path", ""))


# --------------------------------------------------------------------------- #
# scenario runner
def controller_env(cfg: dict, port: int, db_path, base_env: dict | None = None) -> dict:
    """Environment for the harness's controller process.

    The harness owns remediation: either its bundled reference probe or an
    external ``OPERATOR_CMD``. The controller must therefore NOT run its own
    in-process operator, for two reasons:

    * it takes the controller's single-owner operator lease (migration 0007), so
      an external operator -- including the production loop this harness is
      documented to drive -- becomes a permanent standby and the scenario fails
      ``remediation_command_issued`` with count=0; and
    * it targets the default LXD endpoint (:8443), not this run's stub, so any
      action it does take is invisible to the run.

    ``TERMINI_OPERATOR_ENABLED`` defaults to *true* in the controller, so this is
    the only thing that disables the second operator. Keep it that way.
    """
    env = dict(os.environ if base_env is None else base_env)
    env.update({
        "TERMINI_HOST": "127.0.0.1",
        "TERMINI_PORT": str(port),
        "TERMINI_DB_PATH": str(db_path),
        "TERMINI_BOOTSTRAP_TOKEN": cfg["controller"]["bootstrap_token"],
        "TERMINI_PROJECT_TOKEN": cfg["controller"]["project_token"],
        "TERMINI_MIN_REDACTOR_VERSION": "3",
        "PYTHONUNBUFFERED": "1",
        "TERMINI_OPERATOR_ENABLED": "false",
    })
    return env


# --------------------------------------------------------------------------- #
def run_scenario(
    name: str,
    cfg: dict,
    *,
    operator_cmd: str | None = None,
    no_operator: bool = False,
    dry_run: bool | None = None,
    workdir: str | None = None,
    keep: bool = False,
    log=print,
) -> dict:
    scen = config.get_scenario(cfg, name)
    mode = scen["mode"]
    params = scen.get("params", {})
    duration = float(scen.get("duration_s", 8))
    cadence_ms = int(cfg["node"]["cadence_ms"])
    thresholds = cfg["thresholds"]
    expect = scen.get("expect", {})
    expect_remediation = bool(expect.get("remediation"))
    expected_rule = expect.get("rule")
    if dry_run is None:
        dry_run = bool(cfg["operator"].get("dry_run", False))

    tmp = pathlib.Path(workdir) if workdir else pathlib.Path(tempfile.mkdtemp(prefix=f"chaos-{name}-"))
    tmp.mkdir(parents=True, exist_ok=True)
    events: list[tuple[float, str]] = []
    t0 = time.monotonic()

    def mark(msg: str) -> None:
        events.append((time.monotonic() - t0, msg))
        log(f"  [+{time.monotonic() - t0:5.1f}s] {msg}")

    result: dict = {"scenario": name, "mode": mode, "checks": {}, "timeline": [], "artifacts": {}}
    controller = stub = node = operator = None
    controller_port = stub_port = 0
    series_out = tmp / "node_series.json"
    lxd_log = tmp / "lxd_commands.jsonl"
    summary: dict = {}
    vm_id: str | None = None

    try:
        # --- controller ----------------------------------------------------- #
        controller_port = _free_port()
        ctrl_env = controller_env(cfg, controller_port, tmp / "controller.db")
        controller = _popen([sys.executable, "-m", "app.main"], tmp / "controller.log", env=ctrl_env,
                            cwd=CONTROLLER_DIR)
        ctrl_url = f"http://127.0.0.1:{controller_port}"
        mark(f"controller starting on :{controller_port}")
        if not common.wait_for_http(f"{ctrl_url}/v1/nodes/register", attempts=100, delay=0.1):
            raise RuntimeError("controller did not become ready")
        mark("controller ready")

        # --- LXD stub ------------------------------------------------------- #
        lxd_url = config.lxd_url(cfg)
        if cfg["lxd"]["mode"] == "stub":
            stub_port = _free_port()
            lxd_log = tmp / "lxd_commands.jsonl"
            stub = _popen([
                sys.executable, str(HERE / "lxd_stub.py"),
                "--host", "127.0.0.1", "--port", str(stub_port),
                "--log", str(lxd_log),
                "--mutation-status", str(cfg["lxd"].get("mutation_status", "sync")),
            ], tmp / "lxd_stub.log")
            lxd_url = f"http://127.0.0.1:{stub_port}"
            if not common.wait_for_http(f"{lxd_url}/__chaos__/health", attempts=100, delay=0.1):
                raise RuntimeError("lxd stub did not become ready")
            mark(f"lxd stub ready on :{stub_port} (mode=stub)")
        else:
            lxd_log = tmp / "lxd_commands.jsonl"
            mark(f"using real LXD at {lxd_url} (commands not recorded by the harness)")
        result["artifacts"]["lxd_url"] = lxd_url

        # --- node sim ------------------------------------------------------- #
        summary_out = tmp / "node_summary.json"
        series_out = tmp / "node_series.json"
        node = _popen([
            sys.executable, str(HERE / "node_sim.py"),
            "--url", ctrl_url,
            "--bootstrap-token", cfg["controller"]["bootstrap_token"],
            "--hostname", str(cfg["node"]["hostname"]),
            "--mode", mode,
            "--duration", str(duration),
            "--cadence-ms", str(cadence_ms),
            "--params", json.dumps(params),
            "--ram-total-bytes", str(cfg["node"]["ram_total_bytes"]),
            "--disk-total-bytes", str(cfg["node"]["disk_total_bytes"]),
            "--series-out", str(series_out),
            "--summary-out", str(summary_out),
            *(["--gpu"] if cfg["node"].get("gpu") else []),
        ], tmp / "node_sim.log")

        summary: dict = {}
        for _ in range(100):
            if summary_out.exists():
                try:
                    summary = common.read_json(summary_out)
                except (json.JSONDecodeError, OSError):
                    summary = {}
                if summary.get("node_id"):
                    break
            time.sleep(0.1)
        node_id = summary.get("node_id")
        vm_id = summary.get("vm_id")
        if not node_id:
            raise RuntimeError("node did not register (no summary written)")
        mark(f"node registered node_id={node_id} role={summary.get('role')} mode={mode}")

        # --- operator ------------------------------------------------------- #
        op_duration = duration + float(cfg["operator"].get("grace_s", 4.0)) + 3.0
        if not no_operator:
            op = operator_cmd or cfg["operator"].get("cmd")
            if op:
                cmdline = str(op).format(
                    controller_url=ctrl_url,
                    lxd_url=lxd_url,
                    node_id=node_id,
                    project_token=cfg["controller"]["project_token"],
                    thresholds_json=json.dumps(thresholds),
                )
                operator = _popen(["/bin/bash", "-lc", cmdline], tmp / "operator.log")
                mark(f"operator (external) started: {op[:60]}")
            else:
                operator = _popen([
                    sys.executable, str(HERE / "reference_operator.py"),
                    "--controller-url", ctrl_url,
                    "--project-token", cfg["controller"]["project_token"],
                    "--node", node_id,
                    "--lxd-url", lxd_url,
                    "--thresholds", json.dumps(thresholds),
                    "--poll-interval", str(cfg["operator"].get("poll_interval_s", 1.0)),
                    "--duration", str(op_duration),
                    "--audit-out", str(tmp / "operator_audit.jsonl"),
                    *(["--dry-run"] if dry_run else []),
                ], tmp / "operator.log")
                mark("reference operator probe started")

        # --- wait for the scenario to finish -------------------------------- #
        node.wait(timeout=max(30.0, duration + 20.0))
        mark("node telemetry stream finished")
        time.sleep(float(cfg["operator"].get("grace_s", 4.0)))
        mark("grace elapsed (operator polling window)")
    except Exception as exc:  # noqa: BLE001 — surface as a failed scenario
        result["error"] = str(exc)
        mark(f"ERROR {exc}")
    finally:
        _terminate(operator)
        _terminate(node)
        _terminate(stub)
        _terminate(controller)

    # ------------------------------------------------------------------ evaluate
    series = common.read_json(series_out) if series_out.exists() else []
    evaluation = common.evaluate_series(series, thresholds) if series else {}
    commands = _read_commands(lxd_log) if lxd_log.exists() else []
    target_cmds = [c for c in commands if _matches_target(c, vm_id)]

    if expected_rule:
        injection_ok = bool(evaluation.get(expected_rule, {}).get("fired"))
    else:  # healthy: no rule may fire
        injection_ok = all(not v.get("fired") for v in evaluation.values()) if evaluation else False

    # The command assertion is only meaningful when a loop ran and the harness
    # could observe its output (bundled stub). Real-LXD runs are injection-only.
    action_enforced = (not no_operator) and (cfg["lxd"]["mode"] == "stub")
    if action_enforced:
        action_ok = (len(target_cmds) >= 1) if expect_remediation else (len(target_cmds) == 0)
        action_note = "enforced"
    else:
        action_ok = True
        action_note = "not enforced (operator skipped or real LXD)"

    result["checks"] = {
        "telemetry_series_emitted": {"ok": len(series) > 0, "count": len(series)},
        "injection_crosses_rule": {"ok": injection_ok, "expected_rule": expected_rule,
                                   "evaluation": evaluation},
        "remediation_command_issued": {"ok": action_ok, "enforced": bool(action_enforced),
                                       "note": action_note, "count": len(target_cmds),
                                       "expected": expect_remediation},
    }
    result["commands"] = target_cmds
    result["node"] = {k: summary.get(k) for k in ("node_id", "role", "vm_id", "samples_posted")}
    result["timeline"] = [{"t": round(t, 2), "event": e} for t, e in events]
    result["artifacts"] = {"workdir": str(tmp), **result["artifacts"]}

    ok_series = result["checks"]["telemetry_series_emitted"]["ok"]
    result["passed"] = bool(ok_series and injection_ok and action_ok)

    if not keep and workdir is None:
        try:
            shutil.rmtree(tmp)
            result["artifacts"]["workdir"] = "(removed)"
        except OSError:
            pass
    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_report(result: dict) -> None:
    print()
    print(f"scenario: {result['scenario']}  ({result['mode']})")
    for t, event in ((e["t"], e["event"]) for e in result["timeline"]):
        print(f"  +{t:5.1f}s  {event}")
    print("  --- checks ---")
    for key, chk in result["checks"].items():
        flag = "PASS" if chk["ok"] else "FAIL"
        extra = {k: v for k, v in chk.items() if k not in ("ok", "evaluation")}
        print(f"  {flag}  {key}  {extra}")
        if key == "injection_crosses_rule":
            for rule, ev in chk.get("evaluation", {}).items():
                print(f"         {rule}: fired={ev['fired']} run={ev['run']} needed={ev['needed']}")
    print(f"  VERDICT: {'PASS' if result['passed'] else 'FAIL'}")
    if result.get("error"):
        print(f"  error: {result['error']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run one chaos scenario end to end")
    ap.add_argument("--scenario", "-s", required=True, help="oom | disk-fail | healthy")
    ap.add_argument("--config", default=None)
    ap.add_argument("--operator-cmd", default=None,
                    help="production operator command; placeholders {controller_url} {lxd_url} {node_id} "
                         "{project_token} {thresholds_json}")
    ap.add_argument("--no-operator", action="store_true", help="injection only; skip the loop")
    ap.add_argument("--dry-run", action="store_true", help="operator logs the command, sends nothing")
    ap.add_argument("--workdir", default=None, help="keep artifacts under this directory")
    ap.add_argument("--keep", action="store_true", help="do not delete the temp workdir")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args(argv)

    cfg = config.load(args.config)
    result = run_scenario(
        args.scenario, cfg,
        operator_cmd=args.operator_cmd,
        no_operator=args.no_operator,
        dry_run=args.dry_run,
        workdir=args.workdir,
        keep=args.keep,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_report(result)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
