#!/usr/bin/env python3
"""boot_latency.py — adversarial boot-to-ready harness (W7 red-team, t_8d243ee1).

Measures, over N runs, the time for a freshly-restored Termini node to reach
ONLINE at the controller (cloud-init activation -> termini-bootstrap
registration), and repeats that under realistic boot-time degradations:

  baseline   no fault (the frozen D3 activation-only cloud-init path)
  delay      a delayed registration/metadata service (fault_proxy)
  refuse     the registration endpoint resets the connection (fault_proxy)
  dnsfail    an unresolvable controller hostname (DNS failure at boot)
  badtoken   a consumed/expired enrollment token (contract §9 row 2)

It runs against a *dedicated* controller instance (default :8099, its own DB and
token list) so the live demo controller is never disturbed.

Every run emits a JSON record; the harness writes runs.json and summary.json.

Since t_ca7fc132 the harness also:
  * pushes the repo's canonical daemon/unit into the node *before* it starts (the
    same file-copy path as `provision-node.sh REFRESH_RUNTIME=1`), so a run
    verifies the committed daemon and not the copy baked into the snapshot
    (`--baked` restores the old behaviour; `--daemon-src`/`--unit-src` override);
  * stops early and records `reached_terminal` / `terminal_state` / `fail_fast_s`
    when a node parks in a terminal state (`REGISTRATION_FAILED`), so the fail-fast
    paths (badtoken / dnsfail / refuse) are asserted directly;
  * uses an absolute controller DB path (the controller runs with cwd=controller/,
    so a relative `--out` used to resolve its DB somewhere the cleanup could not
    reach, leaving a stale project that 403'd the next run);
  * reports `register_ms` (the daemon's own sub-second §12.5 metric) per run.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import random
import re
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[2]
CONTROLLER_DIR = REPO / "controller"
sys.path.insert(0, str(CONTROLLER_DIR))
try:
    from app import ulid as _ulid  # noqa: E402
    PROJECT_ID = _ulid.new_ulid()
except Exception:  # noqa: BLE001
    PROJECT_ID = "01J0REDTEAM000000000000AB"
HOST = os.environ.get("TERMINI_HOST", "10.14.100.1")  # lxdbr0 gateway, reachable from nodes
NODE_SNAPSHOT = os.environ.get("REDTEAM_SNAPSHOT", "demo-node-a/unregistered")
PROJECT_TOKEN = "term-redteam-project-token"
# The canonical daemon/unit in this repo (infra/warm/node/* are symlinks to them).
DEFAULT_DAEMON_SRC = REPO / "infra" / "bootstrap" / "node" / "termini-bootstrap.py"
DEFAULT_UNIT_SRC = REPO / "infra" / "bootstrap" / "node" / "termini-bootstrap.service"
# The state a node parks in when registration is terminal (never auto-retried).
TERMINAL_STATES = {"REGISTRATION_FAILED"}


def sh(cmd: list[str], timeout: int = 120, check: bool = False) -> subprocess.CompletedProcess:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        p = subprocess.CompletedProcess(cmd, 124, "", f"timeout after {timeout}s")
    if check and p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(cmd)}\n{p.stderr}")
    return p


def log(msg: str, fh=None) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def rand_token(prefix: str) -> str:
    return prefix + "-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10))


# --------------------------------------------------------------------------- controller
class Controller:
    def __init__(self, port: int, tokens: list[str], run_dir: pathlib.Path):
        self.port = port
        self.tokens = tokens
        self.dir = run_dir
        self.db = run_dir / "controller.db"
        self.log_path = run_dir / "controller.log"
        self.proc = None

    def start(self) -> None:
        # a stale DB from a previous run in the same dir would carry a different
        # project id (ensure_project is get-or-create), breaking auth
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(self.db) + suffix)
            except OSError:
                pass
        env = dict(os.environ)
        env.update({
            "TERMINI_HOST": "0.0.0.0",
            "TERMINI_PORT": str(self.port),
            # Absolute path: the controller runs with cwd=CONTROLLER_DIR, so a
            # relative --out would otherwise resolve its DB under controller/,
            # out of reach of the stale-DB cleanup above.
            "TERMINI_DB_PATH": str(self.db.resolve()),
            "TERMINI_BOOTSTRAP_TOKEN": ",".join(self.tokens),
            "TERMINI_PROJECT_TOKEN": PROJECT_TOKEN,
            "TERMINI_PROJECT_ID": PROJECT_ID,
            "TERMINI_MAX_INFERENCE_NODES": "1",
            "TERMINI_MIN_REDACTOR_VERSION": "3",
            "TERMINI_TELEMETRY_INTERVAL_MS": "1000",
            "TERMINI_OPERATOR_ENABLED": "0",
        })
        self.logf = open(self.log_path, "w")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "app.main"],
            cwd=str(CONTROLLER_DIR), env=env,
            stdout=self.logf, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                if self.up():
                    return
            except Exception:  # noqa: BLE001
                pass
            if self.proc.poll() is not None:
                raise RuntimeError(f"controller exited early rc={self.proc.returncode} see {self.log_path}")
            time.sleep(0.5)
        raise RuntimeError("controller did not come up within 30s")

    def up(self) -> bool:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/v1/projects/{PROJECT_ID}/nodes",
                headers={"Authorization": f"Bearer {PROJECT_TOKEN}"},
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    def nodes(self) -> list[dict]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/projects/{PROJECT_ID}/nodes",
            headers={"Authorization": f"Bearer {PROJECT_TOKEN}"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r).get("nodes", [])

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        try:
            self.logf.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- node
def node_userdata(name: str, controller_url: str, token: str, role: str) -> str:
    gpu = 25769803776 if role == "inference" else 0
    return f"""#cloud-config
write_files:
  - path: /etc/termini/config.yaml
    permissions: '0600'
    owner: root:root
    content: |
      controller_url: {controller_url}
      node_name: {name}
      role_hint: {role}
      telemetry_interval_ms: 1000
      inference_port: 8080
      model_alias: deepseek-flash
      image_version: 2026.10.0
      project_id: {PROJECT_ID}
      gpu_vram_bytes: {gpu}
  - path: /var/lib/termini/enrollment.token
    permissions: '0600'
    owner: root:root
    content: |
      {token}
runcmd:
  - [ systemctl, restart, termini-bootstrap.service ]
"""


def exec_cat(name: str, path: str) -> str:
    p = sh(["lxc", "exec", name, "--", "cat", path], timeout=15)
    return p.stdout


def _parse_iso(ts: str):
    from datetime import datetime, timezone
    ts = ts.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(ts, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _journal_times(journal: str):
    """Return (last_service_start, registered_at) datetimes from a journal dump."""
    svc = None
    reg = None
    for line in journal.splitlines():
        head = line.split(" ", 1)[0]
        ts = _parse_iso(head)
        if ts is None:
            continue
        low = line.lower()
        # systemd logs "Starting ..." *before* the daemon's REGISTERED line and
        # "Started ..." (after READY) *after* it, so the service-start anchor must
        # accept "Starting" or the register step would never resolve.
        if ("starting termini-bootstrap.service" in low
                or "started termini-bootstrap.service" in low):
            svc = ts
        if "registered node_id=" in low and reg is None and svc is not None:
            reg = ts
    return svc, reg


def node_state(name: str) -> dict | None:
    out = exec_cat(name, "/var/lib/termini/state.json")
    try:
        return json.loads(out)
    except Exception:  # noqa: BLE001
        return None


_REG_MS = re.compile(r"register_ms=(\d+)\s+boot_to_registered_ms=(\d+)")


def _journal_reg_metrics(journal: str) -> dict:
    """Extract the daemon's own sub-second `register_ms` / `boot_to_registered_ms`
    from its REGISTERED log line (the journal's second-resolution timestamps are
    too coarse for the <5 s §12.5 budget)."""
    out: dict = {}
    for line in journal.splitlines():
        m = _REG_MS.search(line)
        if m:
            out["register_ms"] = int(m.group(1))
            out["boot_to_registered_ms"] = int(m.group(2))
    return out


def one_boot(name: str, controller_url: str, token: str, role: str, budget_s: float,
             run_dir: pathlib.Path, tag: str, controller: Controller, logf,
             snapshot: str = NODE_SNAPSHOT, daemon_src: pathlib.Path | None = None,
             unit_src: pathlib.Path | None = None) -> dict:
    """Restore a fresh node, start it, and time it to ONLINE or a terminal state.

    When ``daemon_src``/``unit_src`` are given they are pushed into the node
    *before it starts* (the same file-copy path as `provision-node.sh
    REFRESH_RUNTIME=1`), so the run exercises the daemon in this repo rather than
    the copy baked into the snapshot. Pass ``--baked`` to skip the push.
    """
    sh(["lxc", "delete", "--force", name], timeout=60)
    t_copy0 = time.time()
    p = None
    for _attempt in range(3):
        p = sh(["lxc", "copy", snapshot, name], timeout=240)
        if p.returncode == 0:
            break
        sh(["lxc", "delete", "--force", name], timeout=60)
        time.sleep(3)
    if p is None or p.returncode != 0:
        # snapshot may have been re-created/removed by another worker: fall back
        # to a fresh init from the pinned baked image alias
        p = sh(["lxc", "init", "agentic-os/w3", name], timeout=240)
    copy_s = time.time() - t_copy0
    if p.returncode != 0:
        return {"tag": tag, "status": "copy_failed", "copy_s": round(copy_s, 2), "stderr": p.stderr[-500:]}

    sh(["lxc", "config", "set", name, "cloud-init.user-data", node_userdata(name, controller_url, token, role)], timeout=30)

    # The `unregistered` snapshot may carry a *consumed* identity/token from a
    # previous enrollment (other workers re-snapshot the demo nodes). Force a
    # clean first-boot registration for every run.
    for rel in ("identity/node.json", "state.json", "enrollment.token"):
        sh(["lxc", "file", "delete", f"{name}/var/lib/termini/{rel}"], timeout=20)

    # Push the repo's canonical daemon (and unit) into the *stopped* node so this
    # run verifies committed code, not the stale baked copy. See module docstring.
    pushed = []
    for src, dest in ((daemon_src, "/usr/local/bin/termini-bootstrap"),
                      (unit_src, "/etc/systemd/system/termini-bootstrap.service")):
        if src is None or not pathlib.Path(src).exists():
            continue
        r = sh(["lxc", "file", "push", "--mode", "0755", "--uid", "0", "--gid", "0",
                str(src), f"{name}{dest}"], timeout=30)
        if r.returncode == 0:
            pushed.append(dest)

    t0 = time.time()
    started = False
    start_err = ""
    for _s in range(4):
        sp = sh(["lxc", "start", name], timeout=90)
        if sp.returncode == 0:
            started = True
            break
        start_err = (sp.stderr or "")[-300:]
        time.sleep(5)
    if not started:
        sh(["lxc", "delete", "--force", name], timeout=60)
        return {"tag": tag, "status": "start_failed", "copy_s": round(copy_s, 2),
                "start_to_online_s": None, "stderr": start_err}

    online_at = None
    terminal_at = None
    first_error = None
    failure_kind = None
    last_state = None
    node_id = None
    while time.time() - t0 < budget_s:
        st = node_state(name)
        if st:
            last_state = st.get("state")
            if st.get("last_error") and first_error is None:
                first_error = st.get("last_error")
            if st.get("failure_kind") and failure_kind is None:
                failure_kind = st.get("failure_kind")
            if st.get("node_id"):
                node_id = st.get("node_id")
            if st.get("state") in TERMINAL_STATES:
                # fail-fast path: the daemon has parked in a terminal state and
                # will not auto-retry (F1/F2). Stop early — this is the evidence.
                terminal_at = time.time()
                break
            if st.get("state") == "ONLINE" and st.get("node_id"):
                # require the controller to have a fresh sample for this node
                try:
                    for n in controller.nodes():
                        if n.get("node_id") == st["node_id"]:
                            online_at = time.time()
                            break
                except Exception:  # noqa: BLE001
                    pass
            if online_at:
                break
        time.sleep(0.2)

    start_to_online = round(online_at - t0, 2) if online_at else None
    fail_fast = round(terminal_at - t0, 2) if terminal_at else None
    if online_at:
        status = "online"
    elif terminal_at:
        status = "terminal"
    else:
        status = "timeout"
    rec = {
        "tag": tag,
        "status": status,
        "copy_s": round(copy_s, 2),
        "start_to_online_s": start_to_online,
        "total_s": round((time.time() - t0) + copy_s, 2),
        "last_state": last_state,
        "reached_terminal": bool(terminal_at),
        "fail_fast_s": fail_fast,
        "failure_kind": failure_kind,
        "node_id": node_id,
        "first_error": first_error,
        "budget_s": budget_s,
        "daemon_pushed": bool(daemon_src),
        "pushed": pushed,
        "snapshot": snapshot,
    }
    # capture the bootstrap journal for this boot and derive the activation ->
    # registration time from it (independent of container boot time)
    jp = sh(["lxc", "exec", name, "--", "journalctl", "-u", "termini-bootstrap",
             "--no-pager", "-o", "short-iso", "--since", f"@{int(t0)}"], timeout=20)
    (run_dir / f"boot-{tag}.journal.txt").write_text(jp.stdout)
    svc_start, reg_at = _journal_times(jp.stdout)
    if svc_start and reg_at:
        rec["service_start_to_register_s"] = round((reg_at - svc_start).total_seconds(), 2)
    rec.update(_journal_reg_metrics(jp.stdout))
    st = node_state(name)
    if st:
        (run_dir / f"boot-{tag}.state.json").write_text(json.dumps(st, indent=2))
    log(f"  boot {tag}: status={rec['status']} copy={rec['copy_s']}s start_to_online={start_to_online}s "
        f"svc_to_register={rec.get('service_start_to_register_s')}s state={last_state} err={first_error}", logf)
    sh(["lxc", "stop", name, "--force"], timeout=60)
    sh(["lxc", "delete", "--force", name], timeout=60)
    return rec


def wait_port(port: int, budget: float = 10.0) -> bool:
    import socket
    end = time.time() + budget
    while time.time() < end:
        s = socket.socket()
        s.settimeout(1)
        try:
            s.connect(("127.0.0.1", port))
            s.close()
            return True
        except OSError:
            time.sleep(0.2)
        finally:
            try:
                s.close()
            except OSError:
                pass
    return False


def free_port(pref: int) -> int:
    import socket
    for p in range(pref, pref + 60):
        s = socket.socket()
        try:
            s.bind(("0.0.0.0", p))
            s.close()
            return p
        except OSError:
            s.close()
    raise RuntimeError(f"no free port near {pref}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--controller-port", type=int, default=8097)
    ap.add_argument("--proxy-port", type=int, default=8096)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--scenario", default="all",
                    choices=["all", "baseline", "delay", "refuse", "dnsfail", "badtoken"])
    ap.add_argument("--delay-s", type=float, default=30.0, help="delay for the 'delay' scenario")
    ap.add_argument("--budget", type=float, default=60.0, help="per-boot budget before TIMEOUT")
    ap.add_argument("--demo-timeout", type=float, default=30.0, help="demo reliability budget (s)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--snapshot", default=NODE_SNAPSHOT,
                    help="LXD snapshot to restore each run from (default %(default)s)")
    ap.add_argument("--daemon-src", default=str(DEFAULT_DAEMON_SRC),
                    help="daemon pushed into the node before start (repo canonical)")
    ap.add_argument("--unit-src", default=str(DEFAULT_UNIT_SRC),
                    help="systemd unit pushed into the node before start")
    ap.add_argument("--baked", action="store_true",
                    help="do NOT push the repo daemon; test the copy baked in the snapshot")
    args = ap.parse_args()
    args.controller_port = free_port(args.controller_port)
    args.proxy_port = free_port(args.proxy_port + 100)
    daemon_src = None if args.baked else pathlib.Path(args.daemon_src)
    unit_src = None if args.baked else pathlib.Path(args.unit_src)

    run_dir = pathlib.Path(args.out)
    run_dir.mkdir(parents=True, exist_ok=True)
    logf = open(run_dir / "boot_harness.log", "w")

    # one dedicated single-use token per run
    total_runs = args.runs * 5
    tokens = [rand_token(f"rt{i}") for i in range(total_runs + 20)]

    ctl = Controller(args.controller_port, tokens, run_dir)
    log(f"starting dedicated controller on :{args.controller_port} db={ctl.db}", logf)
    ctl.start()

    scenarios = []
    if args.scenario in ("all", "baseline"):
        scenarios += [("baseline", args.runs)]
    if args.scenario in ("all", "delay", "refuse"):
        if args.scenario in ("all", "delay"):
            scenarios += [("delay", args.runs)]
        if args.scenario in ("all", "refuse"):
            scenarios += [("refuse", args.runs)]
    if args.scenario in ("all", "dnsfail", "badtoken"):
        if args.scenario in ("all", "dnsfail"):
            scenarios += [("dnsfail", 1)]
        if args.scenario in ("all", "badtoken"):
            scenarios += [("badtoken", 1)]

    def start_proxy(mode: str) -> subprocess.Popen:
        p = subprocess.Popen(
            [sys.executable, str(HERE / "fault_proxy.py"),
             "--listen-port", str(args.proxy_port), "--upstream-port", str(args.controller_port)],
            env={**os.environ, "FAULT_MODE": mode, "DELAY_S": str(args.delay_s)},
            stdout=open(run_dir / f"fault_proxy.{mode}.log", "w"), stderr=subprocess.STDOUT,
        )
        wait_port(args.proxy_port)
        return p

    records = []
    idx = 0
    for scenario, n in scenarios:
        proxy = None
        if scenario in ("delay", "refuse"):
            proxy = start_proxy("delay" if scenario == "delay" else "refuse")
        for i in range(n):
            idx += 1
            name = f"rt-{scenario}-{i + 1}"
            if scenario == "delay":
                url = f"http://{HOST}:{args.proxy_port}"
                tok = tokens[args.runs + i]
            elif scenario == "refuse":
                url = f"http://{HOST}:{args.proxy_port}"
                tok = tokens[args.runs + args.runs + i]
            elif scenario == "dnsfail":
                url = "http://termini-controller.invalid:8099"
                tok = tokens[-3]
            elif scenario == "badtoken":
                url = f"http://{HOST}:{args.controller_port}"
                tok = tokens[-1]
                # consume the single-use token with a throwaway boot first, so the
                # measured boot exercises the contract §9 row-2 path (401)
                one_boot("rt-badtoken-warmup", url, tok, "compute", args.budget,
                         run_dir, "badtoken-warmup", ctl, logf,
                         snapshot=args.snapshot, daemon_src=daemon_src, unit_src=unit_src)
            else:
                url = f"http://{HOST}:{args.controller_port}"
                tok = tokens[i]
            rec = one_boot(name, url, tok, "compute", args.budget, run_dir,
                           f"{scenario}-{i + 1}", ctl, logf,
                           snapshot=args.snapshot, daemon_src=daemon_src, unit_src=unit_src)
            rec["scenario"] = scenario
            rec["controller_url"] = url
            records.append(rec)
        if proxy:
            proxy.terminate()
            try:
                proxy.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proxy.kill()

    ctl.stop()

    # summary
    def stats(vals):
        vals = [v for v in vals if v is not None]
        if not vals:
            return {}
        vals = sorted(vals)
        n = len(vals)
        return {"n": n, "min": vals[0], "max": vals[-1],
                "mean": round(sum(vals) / n, 2),
                "p50": vals[n // 2], "p95": vals[min(n - 1, int(n * 0.95))]}

    summary = {"runs": records, "by_scenario": {}, "demo_timeout_s": args.demo_timeout,
               "daemon_src": str(daemon_src) if daemon_src else None,
               "snapshot": args.snapshot}
    for scenario, _ in scenarios:
        rs = [r for r in records if r.get("scenario") == scenario]
        v = [r.get("start_to_online_s") for r in rs]
        vr = [r.get("service_start_to_register_s") for r in rs]
        ff = [r.get("fail_fast_s") for r in rs]
        rms = [r.get("register_ms") for r in rs]
        ok = [r for r in rs if r.get("status") == "online"]
        term = [r for r in rs if r.get("reached_terminal")]
        states = sorted({r.get("last_state") for r in rs if r.get("last_state")})
        summary["by_scenario"][scenario] = {
            "attempts": len(rs),
            "online": len(ok),
            "terminal": len(term),
            "terminal_states": states,
            "fail_fast_s": stats(ff),
            "register_ms": stats(rms),
            "start_to_online_s": stats(v),
            "service_start_to_register_s": stats(vr),
            "within_demo_timeout": all((x is not None and x <= args.demo_timeout) for x in v) if v else False,
        }
    # Acceptance roll-up for t_ca7fc132: the degraded scenarios must all end in a
    # *terminal* state (not a silent REGISTERING hang), and the happy path online.
    degraded = [s for s in ("badtoken", "dnsfail", "refuse") if any(
        r.get("scenario") == s for r in records)]
    summary["acceptance"] = {
        "degraded_scenarios": degraded,
        "degraded_all_terminal": all(
            summary["by_scenario"][s]["terminal"] == summary["by_scenario"][s]["attempts"]
            for s in degraded) if degraded else None,
        "baseline_all_online": (summary["by_scenario"].get("baseline", {}).get("online") ==
                                summary["by_scenario"].get("baseline", {}).get("attempts")
                                if "baseline" in summary["by_scenario"] else None),
        "baseline_within_demo_timeout": summary["by_scenario"].get("baseline", {}).get("within_demo_timeout"),
    }
    (run_dir / "runs.json").write_text(json.dumps(records, indent=2))
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    log(f"SUMMARY: {json.dumps(summary['by_scenario'])}", logf)
    logf.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
