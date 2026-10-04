#!/usr/bin/env python3
"""OOM race-condition red-team harness (Termini W7, task t_3dc57f17).

Reproduces and measures the two interleavings hypothesised unsafe for the demo:

  H1  detection-vs-kill (the flagged spec race, termini/project.md §4)
      A memory ramp crosses the frozen OOM threshold (ram >= 95 % for
      `oom_consecutive` samples). If the kernel OOM-killer releases the memory
      before the controller has ingested `oom_consecutive` breaching samples and
      the operator has fired, the AI operator never issues a remediation — the
      demo's "self-healing" beat is lost. Modelled with `oom_guest.py`, which
      stops breaching the instant the modelled kernel kills the workload.

  H2  concurrent remediation (idempotency under two triggers)
      Two operator instances (or two in-process loops) observe the same armed
      latch and both issue an LXD command. The schema claims
      `remediation_audits.idempotency_key UNIQUE` + one-decision-per-event make
      this impossible; this measures whether a command can still escape twice.

Both run against the **production** operator code (`controller/app/operator/`)
and a recording LXD REST stub, so any LXD command counted is a real HTTP
mutation, not a log line.

Usage:
    python3 w7/redteam/oom/oom_race.py detect-vs-kill --runs 10
    python3 w7/redteam/oom/oom_race.py concurrency --runs 10
    python3 w7/redteam/oom/oom_race.py all --out-dir /tmp/oom-evidence
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[2]
CONTROLLER_DIR = REPO / "controller"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "controller"))

# --- bootstrap tokens for the isolated stack (not secrets: local temp DB) ----
BOOTSTRAP = "rt-oom-bootstrap"
PROJECT_TOKEN = "rt-oom-project-token"
PROJECT_ID = "01J0DEM0WARM000000000000AB"  # valid ULID (Crockford: no I/L/O/U)
RAM_TOTAL = 34_359_738_368
DEFAULT_THRESHOLDS = {
    "oom_ram_pct": 95.0,
    "oom_consecutive": 2,
    "disk_free_pct": 5.0,
    "disk_free_consecutive": 3,
    "disk_write_latency_ms": 50.0,
    "disk_latency_consecutive": 5,
    "latency_ms": 250.0,
    "latency_consecutive": 3,
}


# --------------------------------------------------------------------------- #
# process / port helpers
# --------------------------------------------------------------------------- #
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def popen(argv, log_path: pathlib.Path, env: dict | None = None,
          cwd: pathlib.Path | None = None) -> subprocess.Popen:
    fh = open(log_path, "wb")
    return subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT, env=env,
                            start_new_session=True, cwd=str(cwd or REPO))


def terminate(proc: subprocess.Popen | None) -> None:
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


def wait_http(url: str, attempts: int = 200, delay: float = 0.1, token: str | None = None) -> bool:
    import urllib.error
    import urllib.request
    for _ in range(attempts):
        try:
            req = urllib.request.Request(url, method="GET")
            if token:
                req.add_header("Authorization", "Bearer " + token)
            with urllib.request.urlopen(req, timeout=2):
                return True
        except urllib.error.HTTPError:
            return True  # any HTTP answer means the server is up
        except Exception:
            time.sleep(delay)
    return False


def read_jsonl(path: pathlib.Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


# --------------------------------------------------------------------------- #
# isolated stack
# --------------------------------------------------------------------------- #
class Stack:
    """A temp controller + recording LXD stub, torn down on exit."""

    def __init__(self, workdir: pathlib.Path, *, cadence_ms: int = 1000,
                 enable_operator: bool = False,
                 operator_poll_s: float = 1.0):
        self.workdir = workdir
        self.cadence_ms = cadence_ms
        self.enable_operator = enable_operator
        self.operator_poll_s = operator_poll_s
        self.controller = None
        self.stub = None
        self.controller_url = None
        self.stub_url = None
        self.lxd_log = workdir / "lxd_commands.jsonl"

    def __enter__(self) -> "Stack":
        self.workdir.mkdir(parents=True, exist_ok=True)
        # Fresh command log for this run: the stub appends, so a leftover file from
        # a previous run in the same workdir would double-count commands.
        try:
            self.lxd_log.unlink()
        except FileNotFoundError:
            pass
        # LXD stub first: the controller needs its URL at launch so its in-process
        # operator (when enabled) can reach it.
        sport = free_port()
        self.stub = popen([sys.executable, str(REPO / "chaos" / "lxd_stub.py"),
                           "--host", "127.0.0.1", "--port", str(sport),
                           "--log", str(self.lxd_log)],
                          self.workdir / "lxd_stub.log")
        self.stub_url = f"http://127.0.0.1:{sport}"
        if not wait_http(f"{self.stub_url}/__chaos__/health"):
            raise RuntimeError("lxd stub did not become ready")

        self.port = free_port()
        env = dict(os.environ)
        env.update({
            "TERMINI_HOST": "127.0.0.1",
            "TERMINI_PORT": str(self.port),
            "TERMINI_DB_PATH": str(self.workdir / "controller.db"),
            "TERMINI_BOOTSTRAP_TOKEN": BOOTSTRAP,
            "TERMINI_PROJECT_TOKEN": PROJECT_TOKEN,
            "TERMINI_PROJECT_ID": PROJECT_ID,
            "TERMINI_MIN_REDACTOR_VERSION": "3",
            "TERMINI_TELEMETRY_INTERVAL_MS": str(self.cadence_ms),
            # When disabled, exactly ONE operator (the standalone CLI under test)
            # acts on the telemetry; otherwise the shipped in-process loop and the
            # standalone loop would both fire.
            "TERMINI_OPERATOR_ENABLED": "1" if self.enable_operator else "0",
            "TERMINI_OPERATOR_POLL_INTERVAL_S": str(self.operator_poll_s),
            "TERMINI_LXD_BASE_URL": self.stub_url,
            "PYTHONUNBUFFERED": "1",
        })
        self.env = env
        self.controller = self._launch_controller()
        self.controller_url = f"http://127.0.0.1:{self.port}"
        if not wait_http(f"{self.controller_url}/v1/nodes/register"):
            raise RuntimeError("controller did not become ready")
        return self

    def _launch_controller(self) -> subprocess.Popen:
        return popen([sys.executable, "-m", "app.main"],
                     self.workdir / "controller.log", env=self.env, cwd=CONTROLLER_DIR)

    def restart_controller(self) -> None:
        """SIGKILL + relaunch the controller on the same DB (forced-crash recovery
        test). The LXD stub and its recorded commands are untouched. A fresh port
        is used so a lingering TIME_WAIT bind can never be mistaken for a crash."""
        if self.controller is not None:
            try:
                os.killpg(os.getpgid(self.controller.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.controller.wait(timeout=5)
        self.port = free_port()
        self.env["TERMINI_PORT"] = str(self.port)
        self.controller_url = f"http://127.0.0.1:{self.port}"
        self.controller = self._launch_controller()
        if not wait_http(f"{self.controller_url}/v1/nodes/register"):
            raise RuntimeError("controller did not come back after restart")
        time.sleep(0.3)

    def __exit__(self, *exc) -> None:
        terminate(self.controller)
        terminate(self.stub)

    def commands(self) -> list[dict]:
        return [e for e in read_jsonl(self.lxd_log)
                if e.get("method") in ("POST", "PUT", "PATCH", "DELETE")
                and str(e.get("path", "")).startswith("/1.0")]


def start_guest(stack: Stack, workdir: pathlib.Path, *, ramp_s: float, peak_pct: float,
                hold_s: float, kill_pct: float | None, tag: str,
                cadence_ms: int = 1000) -> tuple[subprocess.Popen, dict, pathlib.Path, pathlib.Path]:
    """Launch oom_guest.py and wait until it has registered (node_id available)."""
    summary_out = workdir / f"{tag}_summary.json"
    kill_out = workdir / f"{tag}_kill.json"
    series_out = workdir / f"{tag}_series.json"
    # Remove stale artifacts from any previous run in the same workdir, so the
    # registration wait below never reads a previous guest's summary.
    for stale in (summary_out, kill_out, series_out):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
    argv = [
        sys.executable, str(HERE / "oom_guest.py"),
        "--url", stack.controller_url,
        "--bootstrap-token", BOOTSTRAP,
        "--hostname", f"node-{tag}",
        "--ram-start-pct", "62", "--ram-peak-pct", str(peak_pct),
        "--ramp-s", str(ramp_s), "--hold-s", str(hold_s),
        "--cadence-ms", str(cadence_ms),
        "--series-out", str(series_out), "--kill-out", str(kill_out),
        "--summary-out", str(summary_out),
    ]
    if kill_pct is None:
        argv.append("--no-kill")
    else:
        argv += ["--kill-pct", str(kill_pct)]
    proc = popen(argv, workdir / f"{tag}_guest.log")
    summary: dict = {}
    for _ in range(200):
        if summary_out.exists():
            try:
                summary = json.loads(summary_out.read_text())
            except json.JSONDecodeError:
                summary = {}
            if summary.get("node_id"):
                break
        time.sleep(0.05)
    if not summary.get("node_id"):
        raise RuntimeError("guest did not register")
    return proc, summary, summary_out, kill_out


def start_operator(stack: Stack, workdir: pathlib.Path, node_id: str, *,
                   duration_s: float, poll_s: float = 1.0, tag: str,
                   thresholds: dict | None = None,
                   instance_names: dict | None = None,
                   state_file: pathlib.Path | None = None) -> tuple[subprocess.Popen, pathlib.Path, pathlib.Path]:
    audit = workdir / f"{tag}_audit.jsonl"
    st = state_file or (workdir / f"{tag}_state.json")
    argv = [
        sys.executable, str(CONTROLLER_DIR / "app" / "operator" / "cli.py"),
        "--controller-url", stack.controller_url,
        "--project-token", PROJECT_TOKEN,
        "--node", node_id,
        "--lxd-url", stack.stub_url,
        "--thresholds", json.dumps(thresholds or DEFAULT_THRESHOLDS),
        "--instance-names", json.dumps(instance_names or {"oom-race-vm": "agent-c1"}),
        "--default-instance-name", "agent",
        "--poll-interval", str(poll_s),
        "--duration", str(duration_s),
        "--audit-out", str(audit),
        "--state-file", str(st),
    ]
    proc = popen(argv, workdir / f"{tag}_operator.log")
    return proc, audit, st


# --------------------------------------------------------------------------- #
# H1 — detection-vs-kill
# --------------------------------------------------------------------------- #
def scenario_detect_vs_kill(workdir: pathlib.Path, *, ramp_s: float, peak_pct: float,
                            hold_s: float, kill_pct: float | None, drain_s: float = 3.0,
                            cadence_ms: int = 1000) -> dict:
    """One full isolated pass. Returns a record with the observed interleaving."""
    with Stack(workdir, cadence_ms=cadence_ms) as stack:
        guest, gsum, summary_out, kill_out = start_guest(
            stack, workdir, ramp_s=ramp_s, peak_pct=peak_pct, hold_s=hold_s,
            kill_pct=kill_pct, tag="g", cadence_ms=cadence_ms)
        node_id = gsum["node_id"]
        total_s = ramp_s + hold_s
        op, audit_path, _ = start_operator(
            stack, workdir, node_id, duration_s=total_s + drain_s + 3.0, tag="op",
            instance_names={gsum["vm_id"]: "agent-c1"})
        try:
            guest.wait(timeout=total_s + 15.0)
        except subprocess.TimeoutExpired:
            pass
        # Give the operator the post-guest window to poll whatever telemetry remains.
        time.sleep(drain_s)
        terminate(op)
        time.sleep(0.3)
        kill = json.loads(kill_out.read_text()) if kill_out.exists() else {}
        audits = read_jsonl(audit_path)
        commands = stack.commands()
        oom_fires = [a for a in audits if a.get("rule") == "oom-guard-01"]
        restart_cmds = [c for c in commands
                        if c.get("method") == "PUT" and c.get("body", {}).get("action") == "restart"]

        fired_at = oom_fires[0]["fired_at"] if oom_fires else None
        kill_ts = kill.get("kill_ts")
        fired_before_kill = None
        if fired_at is not None and kill_ts is not None:
            fired_before_kill = fired_at <= kill_ts
        elif fired_at is not None and kill_ts is None:
            fired_before_kill = True  # nothing killed; remediation is a clean win

        rec = {
            "ramp_s": ramp_s,
            "peak_pct": peak_pct,
            "hold_s": hold_s,
            "kill_pct": kill_pct,
            "cadence_ms": cadence_ms,
            "killed": bool(kill.get("killed")),
            "kill_ts": kill_ts,
            "kill_seq": kill.get("kill_seq"),
            "last_breach_seq": kill.get("last_breach_seq"),
            "breaching_samples": sum(1 for w in kill.get("wall", []) if w["knee"] == "breach"),
            "oom_fired": bool(oom_fires),
            "fired_at": fired_at,
            "detection_latency_s": oom_fires[0].get("detection_latency_s") if oom_fires else None,
            "remediation_latency_s": oom_fires[0].get("remediation_latency_s") if oom_fires else None,
            "audit_result": oom_fires[0].get("result") if oom_fires else None,
            "lxd_restart_commands": len(restart_cmds),
            "fired_before_kill": fired_before_kill,
        }
        # Outcome classification.
        if fired_before_kill is True and len(restart_cmds) >= 1:
            rec["outcome"] = "safe: remediated before kill"
        elif kill.get("killed") and not oom_fires:
            rec["outcome"] = "UNSAFE: kernel killed the workload before the operator fired"
        elif kill.get("killed") and oom_fires and not fired_before_kill:
            rec["outcome"] = "UNSAFE: operator fired only after the workload was killed"
        elif not kill.get("killed") and not oom_fires:
            rec["outcome"] = "UNSAFE: no kill and no fire (detection failed)"
        else:
            rec["outcome"] = "safe"
        return rec


# --------------------------------------------------------------------------- #
# H2 — concurrency / idempotency
# --------------------------------------------------------------------------- #
def scenario_concurrency_processes(workdir: pathlib.Path, *, hold_s: float = 8.0) -> dict:
    """Two independent operator *processes* (distinct latch stores) against one
    node with a sustained, non-killing breach. Counts real LXD restarts."""
    with Stack(workdir) as stack:
        guest, gsum, _, _ = start_guest(
            stack, workdir, ramp_s=3.0, peak_pct=97.0, hold_s=hold_s, kill_pct=None, tag="g")
        node_id = gsum["node_id"]
        d = hold_s + 10.0
        inst = {gsum["vm_id"]: "agent-c1"}
        op1, a1, s1 = start_operator(stack, workdir, node_id, duration_s=d, tag="opA",
                                     instance_names=inst)
        op2, a2, s2 = start_operator(stack, workdir, node_id, duration_s=d, tag="opB",
                                     instance_names=inst)
        try:
            guest.wait(timeout=hold_s + 15.0)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(3.0)
        terminate(op1)
        terminate(op2)
        time.sleep(0.3)
        cmds = stack.commands()
        restarts = [c for c in cmds if c.get("method") == "PUT"
                    and c.get("body", {}).get("action") == "restart"]
        return {
            "mode": "two-processes",
            "restart_commands": len(restarts),
            "auditA_fires": len([a for a in read_jsonl(a1) if a.get("rule") == "oom-guard-01"]),
            "auditB_fires": len([a for a in read_jsonl(a2) if a.get("rule") == "oom-guard-01"]),
            "verdict": "single-remediation" if len(restarts) == 1 else
                       ("NO-remediation" if len(restarts) == 0 else "DOUBLE-remediation"),
        }


def scenario_concurrency_inprocess(workdir: pathlib.Path, *, runs: int = 1) -> dict:
    """Two in-process OperatorLoop instances (same DB, the shipped persistence)
    started from a barrier against one sustained breach. Tests the schema-level
    idempotency claim directly."""
    from types import SimpleNamespace
    from app import db, store, ulid
    from app.operator import Thresholds, OperatorLoop, playbook
    from app.operator.executor import LxdExecutor

    results = []
    for _ in range(runs):
        dbb = workdir / f"conc_{ulid.new_ulid()}.db"
        if dbb.exists():
            dbb.unlink()
        db.migrate(str(dbb))
        # recording stub in-process
        from tests.stub_lxd import StubLxd
        lxd = StubLxd()
        lxd.start()
        try:
            with db.session(str(dbb)) as conn:
                pid = ulid.new_ulid()
                conn.execute(
                    "INSERT INTO projects (id, name, project_token_hash, max_inference_nodes, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (pid, "t", "h", 1, store.now_iso()))
                nid = ulid.new_ulid()
                conn.execute(
                    "INSERT INTO nodes (id, project_id, registration_id, machine_id, hostname_hash,"
                    " role, node_token_hash, hardware_json, os_json, lxd_json, role_hint,"
                    " installer_version, status, last_seen_at, registered_at, source_ip)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (nid, pid, "r1", None, "hh", "compute", "th", "{}", None, None, "auto",
                     "0.1", "online", store.now_iso(), store.now_iso(), None))
                vm_id = "oom-race-vm"
                samples = []
                for i in range(6):
                    samples.append({
                        "seq": 100 + i,
                        "ts": f"2026-10-04T08:00:{i:02d}.000Z",
                        "ram_total_bytes": RAM_TOTAL,
                        "ram_used_bytes": int(RAM_TOTAL * 0.97),
                        "disk": [{"path": "/dev/sda", "total_bytes": 274_877_906_944,
                                  "free_bytes": 200_000_000_000, "write_latency_ms": 3.0,
                                  "mount_ro": False}],
                        "net": [{"iface": "eth0", "rx_bytes": 1, "tx_bytes": 1, "latency_ms": 0.8}],
                        "lxd_instances": [{"vm_id": vm_id, "state": "running",
                                           "ram_used_bytes": 10_000_000_000,
                                           "ram_limit_bytes": 16_000_000_000,
                                           "disk_used_bytes": 1_000_000_000}],
                    })
                store.insert_samples(conn, nid, ulid.new_ulid(), samples)
            cfg = SimpleNamespace(
                db_path=str(dbb), operator_poll_interval_s=1.0, operator_cooldown_s=5.0,
                operator_window=60, lxd_base_url=lxd.base_url, operator_dry_run=False,
                lxd_request_timeout_s=5.0, lxd_retry_max=2, lxd_retry_backoff_s=0.01,
                lxd_client_cert=None, lxd_client_key=None)
            playbooks = playbook.load_playbooks(str(CONTROLLER_DIR / "playbooks"))
            barrier = threading.Barrier(2)
            produced = []

            def worker(n):
                loop = OperatorLoop(cfg, {"id": pid}, Thresholds(), playbooks,
                                    instance_names={vm_id: "agent-c1"},
                                    default_instance_name="agent",
                                    lxd=LxdExecutor(lxd.base_url, retry_max=2, backoff_s=0.01))
                barrier.wait()
                try:
                    produced.append(loop.run_once())
                except Exception as exc:  # noqa: BLE001
                    produced.append([{"error": f"{type(exc).__name__}: {exc}"}])

            ts = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(timeout=20)
            restarts = len(lxd.restart_requests())
            with db.session(str(dbb)) as conn:
                audits = conn.execute("SELECT COUNT(*) c FROM remediation_audits").fetchone()["c"]
                decisions = conn.execute("SELECT COUNT(*) c FROM remediation_decisions").fetchone()["c"]
            results.append({
                "restart_commands": restarts,
                "remediation_audits": audits,
                "remediation_decisions": decisions,
                "worker_errors": [p[0] for p in produced if p and isinstance(p[0], dict) and "error" in p[0]],
                "verdict": "single-remediation" if restarts == 1 else
                           ("NO-remediation" if restarts == 0 else "DOUBLE-remediation"),
            })
        finally:
            lxd.stop()
    agg = {
        "mode": "two-inprocess-loops",
        "runs": runs,
        "double_remediation_runs": sum(1 for r in results if r["verdict"] == "DOUBLE-remediation"),
        "records": results,
    }
    return agg


def scenario_live_inprocess(workdir: pathlib.Path, *, hold_s: float = 8.0) -> dict:
    """The shipped deployment: the controller's **in-process** OperatorLoop is
    enabled and pointed at the recording LXD stub. Drives one sustained breach and
    asserts the loop fired and issued a real LXD REST command (W6.1 evidence)."""
    import sqlite3
    with Stack(workdir, enable_operator=True, operator_poll_s=1.0) as stack:
        guest, gsum, _, _ = start_guest(
            stack, workdir, ramp_s=3.0, peak_pct=97.0, hold_s=hold_s, kill_pct=None, tag="g")
        try:
            guest.wait(timeout=hold_s + 15.0)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(4.0)  # let the in-process loop poll the held breach
        dbp = workdir / "controller.db"
        events = audits = 0
        fired_at = None
        detection_lat = None
        try:
            conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            ev = conn.execute(
                "SELECT * FROM health_events WHERE rule_id='oom-guard-01'").fetchall()
            events = len(ev)
            if ev:
                fired_at = ev[0]["fired_at"]
                detection_lat = ev[0]["breach_started_at"]
            audits = conn.execute(
                "SELECT COUNT(*) c FROM remediation_audits WHERE rule_id='oom-guard-01'"
            ).fetchone()["c"]
            conn.close()
        except sqlite3.Error as exc:
            return {"error": str(exc)}
        cmds = stack.commands()
        restarts = [c for c in cmds if c.get("method") == "PUT"
                    and c.get("body", {}).get("action") == "restart"]
        return {
            "mode": "in-process shipped loop",
            "operator_poll_s": 1.0,
            "health_events": events,
            "remediation_audits": audits,
            "lxd_restart_commands": len(restarts),
            "fired_at": fired_at,
            "breach_started_at": detection_lat,
            "verdict": "LIVE-TRIGGER-OK" if (events >= 1 and len(restarts) >= 1)
                       else "LIVE-TRIGGER-MISSED",
        }


def cmd_live(args) -> int:
    out = pathlib.Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    wd = _mk(out, "live_inprocess")
    shutil.rmtree(wd, ignore_errors=True)   # fresh DB + command log
    wd = _mk(out, "live_inprocess")
    rec = scenario_live_inprocess(wd)
    (out / "live_inprocess.json").write_text(json.dumps(rec, indent=2))
    print(json.dumps(rec, indent=2))
    shutil.rmtree(wd, ignore_errors=True)
    return 0


def _db_counts(dbp: pathlib.Path) -> dict:
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        events = conn.execute("SELECT COUNT(*) c FROM health_events WHERE rule_id='oom-guard-01'").fetchone()["c"]
        audits = conn.execute("SELECT COUNT(*) c FROM remediation_audits WHERE rule_id='oom-guard-01'").fetchone()["c"]
        st = conn.execute(
            "SELECT fired_count FROM trigger_state WHERE rule_id='oom-guard-01'").fetchall()
        counts = [r["fired_count"] for r in st]
        conn.close()
        return {"health_events": events, "remediation_audits": audits, "fired_counts": counts}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def scenario_mixed_operators(workdir: pathlib.Path, *, hold_s: float = 10.0) -> dict:
    """The realistic deployment hazard: the controller's in-process loop is ON
    (as shipped) *and* a standalone operator (the documented `OPERATOR_CMD`, used
    by the chaos harness / W6.1 evidence) runs against the same node. Both see the
    same armed episode. Counts the real LXD restart commands that escape."""
    with Stack(workdir, enable_operator=True) as stack:
        guest, gsum, _, _ = start_guest(
            stack, workdir, ramp_s=3.0, peak_pct=97.0, hold_s=hold_s, kill_pct=None, tag="g")
        node_id = gsum["node_id"]
        op, audit_path, _ = start_operator(
            stack, workdir, node_id, duration_s=hold_s + 10.0, tag="cli",
            instance_names={gsum["vm_id"]: "agent-c1"})
        try:
            guest.wait(timeout=hold_s + 15.0)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(4.0)
        terminate(op)
        time.sleep(0.3)
        cmds = stack.commands()
        restarts = [c for c in cmds if c.get("method") == "PUT"
                    and c.get("body", {}).get("action") == "restart"]
        db = _db_counts(workdir / "controller.db")
        cli_fires = len([a for a in read_jsonl(audit_path) if a.get("rule") == "oom-guard-01"])
        return {
            "mode": "in-process loop + standalone CLI on the same node",
            "lxd_restart_commands": len(restarts),
            "cli_operator_fires": cli_fires,
            "db": db,
            "verdict": "single-remediation" if len(restarts) == 1 else
                       ("NO-remediation" if len(restarts) == 0 else "DOUBLE-remediation"),
        }


def scenario_restart_recovery(workdir: pathlib.Path, *, hold_s: float = 20.0) -> dict:
    """Guardrail check: kill the controller *while the breach is live*, restart it
    on the same DB, and confirm the persisted latch prevents a second remediation
    (no re-fire of an already-remediated episode)."""
    with Stack(workdir, enable_operator=True) as stack:
        guest, gsum, _, _ = start_guest(
            stack, workdir, ramp_s=3.0, peak_pct=97.0, hold_s=hold_s, kill_pct=None, tag="g")
        before = {}
        for _ in range(60):
            before = _db_counts(workdir / "controller.db")
            if before.get("remediation_audits", 0) >= 1:
                break
            time.sleep(0.5)
        if before.get("remediation_audits", 0) < 1:
            return {"error": "first remediation never fired", "db": before}
        terminate(guest)                      # stop the telemetry stream
        time.sleep(0.3)
        stack.restart_controller()            # SIGKILL + relaunch on the same DB
        time.sleep(6.0)                       # let the fresh loop re-evaluate stored samples
        after = _db_counts(workdir / "controller.db")
        cmds = stack.commands()
        restarts = [c for c in cmds if c.get("method") == "PUT"
                    and c.get("body", {}).get("action") == "restart"]
        recovered = (after.get("remediation_audits", 0) == before.get("remediation_audits", 0)
                     and len(restarts) == 1)
        return {
            "mode": "SIGKILL controller mid-breach -> restart on same DB",
            "audits_before_restart": before.get("remediation_audits"),
            "audits_after_restart": after.get("remediation_audits"),
            "lxd_restart_commands": len(restarts),
            "trigger_state_fired_counts": after.get("fired_counts"),
            "verdict": "state-recovers-no-refire" if recovered else "RE-FIRE-OR-LOST-STATE",
        }


def cmd_resilience(args) -> int:
    out = pathlib.Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    mixed = []
    for i in range(args.runs):
        shutil.rmtree(out / f"mixed_{i}", ignore_errors=True)
        wd = _mk(out, f"mixed_{i}")
        rec = scenario_mixed_operators(wd)
        rec["run"] = i
        mixed.append(rec)
        print(f"  mixed run={i}: {rec['verdict']} (restarts={rec['lxd_restart_commands']})")
        shutil.rmtree(wd, ignore_errors=True)
    recovery = []
    for i in range(min(args.runs, 3)):
        shutil.rmtree(out / f"recovery_{i}", ignore_errors=True)
        wd = _mk(out, f"recovery_{i}")
        rec = scenario_restart_recovery(wd)
        rec["run"] = i
        recovery.append(rec)
        print(f"  recovery run={i}: {rec.get('verdict')}")
        shutil.rmtree(wd, ignore_errors=True)
    result = {"mixed_operators": mixed, "restart_recovery": recovery,
              "double_remediation_runs": sum(1 for r in mixed
                                             if r.get("verdict") == "DOUBLE-remediation")}
    (out / "resilience.json").write_text(json.dumps(result, indent=2))
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _mk(out_dir: pathlib.Path, name: str) -> pathlib.Path:
    p = out_dir / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def cmd_detect_vs_kill(args) -> int:
    out = pathlib.Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    kill_pct = getattr(args, "kill_pct", 100.0)
    control_runs = max(1, getattr(args, "control_runs", 3))
    params = []
    for ramp in args.ramps:
        params.append((ramp, kill_pct, args.runs))          # uncapped -> kernel kill
        params.append((ramp, None, control_runs))           # capped peak (mitigation control)
    records = []
    for ramp, kill, nruns in params:
        for i in range(nruns):
            wd = _mk(out, f"dvk_r{ramp}_k{kill}_{i}")
            peak = 100.0 if kill is not None else 97.0
            hold = 6.0
            rec = scenario_detect_vs_kill(wd, ramp_s=ramp, peak_pct=peak,
                                          hold_s=hold, kill_pct=kill,
                                          cadence_ms=args.cadence_ms)
            rec["run"] = i
            records.append(rec)
            print(f"  ramp={ramp}s kill={kill} run={i}: {rec['outcome']} "
                  f"(killed={rec['killed']} fired={rec['oom_fired']} "
                  f"before_kill={rec['fired_before_kill']} cmds={rec['lxd_restart_commands']})")
            shutil.rmtree(wd, ignore_errors=True)
    (out / "detect_vs_kill.json").write_text(json.dumps(records, indent=2))
    # summarise
    print("\nsummaries:")
    groups: dict = {}
    for r in records:
        groups.setdefault((r["ramp_s"], str(r["kill_pct"])), []).append(r)
    summary = []
    for (ramp, kill), rs in sorted(groups.items()):
        rep = sum(1 for r in rs if r["outcome"].startswith("UNSAFE"))
        summary.append({
            "ramp_s": ramp, "kill_pct": kill, "runs": len(rs),
            "unsafe": rep, "repro_rate": round(rep / len(rs), 3),
            "fires": sum(1 for r in rs if r["oom_fired"]),
        })
        print(f"  ramp={ramp}s kill={kill}: unsafe {rep}/{len(rs)} "
              f"({rep/len(rs):.0%}), fired {summary[-1]['fires']}/{len(rs)}")
    (out / "detect_vs_kill_summary.json").write_text(json.dumps(summary, indent=2))
    return 0


def cmd_concurrency(args) -> int:
    out = pathlib.Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    proc_records = []
    for i in range(args.runs):
        wd = _mk(out, f"conc_proc_{i}")
        rec = scenario_concurrency_processes(wd)
        rec["run"] = i
        proc_records.append(rec)
        print(f"  two-process run={i}: {rec['verdict']} "
              f"(restarts={rec['restart_commands']})")
        shutil.rmtree(wd, ignore_errors=True)
    inproc = scenario_concurrency_inprocess(_mk(out, "inproc"), runs=args.runs)
    print(f"  two-inprocess: double-remediation {inproc['double_remediation_runs']}/{args.runs}")
    result = {"two_process": proc_records, "two_inprocess": inproc}
    (out / "concurrency.json").write_text(json.dumps(result, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Termini OOM race red-team")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detect-vs-kill")
    d.add_argument("--runs", type=int, default=10)
    d.add_argument("--control-runs", type=int, default=3)
    d.add_argument("--ramps", type=float, nargs="+", default=[4.0, 8.0, 20.0])
    d.add_argument("--kill-pct", type=float, default=100.0)
    d.add_argument("--cadence-ms", type=int, default=1000)
    d.add_argument("--out-dir", default="/tmp/oom-evidence")
    d.set_defaults(func=cmd_detect_vs_kill)

    c = sub.add_parser("concurrency")
    c.add_argument("--runs", type=int, default=10)
    c.add_argument("--out-dir", default="/tmp/oom-evidence")
    c.set_defaults(func=cmd_concurrency)

    l = sub.add_parser("live-trigger")
    l.add_argument("--out-dir", default="/tmp/oom-evidence")
    l.set_defaults(func=cmd_live)

    r = sub.add_parser("resilience")
    r.add_argument("--runs", type=int, default=10)
    r.add_argument("--out-dir", default="/tmp/oom-evidence")
    r.set_defaults(func=cmd_resilience)

    a = sub.add_parser("all")
    a.add_argument("--runs", type=int, default=10)
    a.add_argument("--out-dir", default="/tmp/oom-evidence")
    a.add_argument("--ramps", type=float, nargs="+", default=[4.0, 8.0, 20.0])
    a.add_argument("--cadence-ms", type=int, default=1000)
    a.set_defaults(func=None)

    args = ap.parse_args(argv)
    # Make the output dir absolute: the controller is launched with cwd=controller/
    # so a relative TERMINI_DB_PATH would resolve differently there than in this
    # harness (which would then fail to find the DB it just created).
    if getattr(args, "out_dir", None):
        args.out_dir = str(pathlib.Path(args.out_dir).resolve())
    if args.cmd == "all":
        rc = cmd_detect_vs_kill(args)
        rc |= cmd_concurrency(args)
        rc |= cmd_live(args)
        rc |= cmd_resilience(args)
        return rc
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
