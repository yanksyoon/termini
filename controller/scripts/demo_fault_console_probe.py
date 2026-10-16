#!/usr/bin/env python3
"""demo_fault_console_probe.py — end-to-end acceptance probe for the operator fault
console (`docs/contracts/controller-api.md` §4.7).

Boots a throwaway controller (this checkout's build) on a free port with a
scratch DB, registers two nodes (one real-provenance, one `synthetic`), then
drives the fault-console endpoints the way the browser UI does and prints the raw
evidence: the injected series, the rule firing, the operator log lines, the
decision/audit the AI operator wrote, the guard refusals, the recovery, and the
TTL sweep.

    python3 controller/scripts/demo_fault_console_probe.py            # all beats
    python3 controller/scripts/demo_fault_console_probe.py --keep     # leave the DB

Beats (matching the card's acceptance list):
  a. inject OOM on a chosen node -> series ramps, rule fires, log line, decision
  b. clear it -> healthy series restored, rule re-arms below threshold
  c. guards: a second injection for the same (node, rule) is refused 409
     `fault_already_active`; an injection right after a remediation for that rule is
     refused 409 `remediation_in_flight`
  d. auth: no token -> 401, node token -> 403 (`not reachable from a node context`)
  e. TTL: a live injection ends by itself, loudly, at its TTL (never silently)

Exit code 0 only when every beat passed.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[2]
L4_24GB = 24 * 1024 ** 3
GiB = 1024 ** 3

FAILURES: list[str] = []


# --------------------------------------------------------------------------- #
# tiny HTTP helper (stdlib only, like the seeder)
# --------------------------------------------------------------------------- #
def call(base: str, method: str, path: str, body=None, token: str | None = None,
         timeout: float = 15.0) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        finally:
            e.close()
        try:
            return e.code, (json.loads(raw) if raw else None)
        except json.JSONDecodeError:
            return e.code, raw.decode(errors="replace")


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)
    return ok


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_up(base: str, timeout_s: float = 20.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/v1/telemetry/recent", timeout=2) as r:
                return True
        except urllib.error.HTTPError:
            return True            # an HTTP answer of any kind means "serving"
        except Exception:          # noqa: BLE001
            time.sleep(0.2)
    return False


# --------------------------------------------------------------------------- #
# node fixtures
# --------------------------------------------------------------------------- #
def registration_body(hostname: str, *, synthetic: bool, ram_bytes: int = 8 * GiB) -> dict:
    return {
        "schema_version": "1.0",
        "synthetic": synthetic,
        "machine": {
            "machine_id": hostname,
            "hostname": hostname,
            "cpu": {"model": "probe", "cores": 4, "threads": 8},
            "memory": {"total_bytes": ram_bytes},
            "disks": [{"device": "/dev/sda", "size_bytes": 100 * GiB}],
            "gpus": [],
        },
    }


def healthy_sample(node_id: str, project_id: str, seq: int) -> dict:
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    return {
        "seq": seq,
        "ts": now,
        "uptime_sec": 3600 + seq,
        "cpu_util_pct": 31.2,
        "cpu_core_count": 8,
        "ram_total_bytes": 8 * GiB,
        "ram_used_bytes": int(8 * GiB * 0.62),
        "hostname": "5f2a9c1e7b4d4a1f",
        "ip_local": "10.0.0.0/24",
        "mac_address": "9c1e7b4d4a1f5f2a",
        "project_id": project_id,
        "disk": [{"path": "/dev/sda", "total_bytes": 100 * GiB,
                  "free_bytes": int(100 * GiB * 0.78), "write_latency_ms": 3.0, "mount_ro": False}],
        "net": [{"iface": "eth0", "rx_bytes": 1000, "tx_bytes": 1000, "latency_ms": 0.8}],
        "services": [{"name": "termini-bootstrap", "status": "running"},
                     {"name": "lxd", "status": "running"},
                     {"name": "sshd", "status": "running"}],
        "lxd_instances": [{"vm_id": "01M4AAAAAAAAAAAAAAAAAAAAAA", "state": "running",
                           "cpu_util_pct": 12.0, "ram_used_bytes": 1 * GiB,
                           "ram_limit_bytes": 4 * GiB, "disk_used_bytes": 2 * GiB}],
    }


def envelope(node_id: str, sample: dict, call_id: str) -> dict:
    return {
        "_redaction": {"version": "redactor-config v3", "config_hash": "3f2a1b", "result": "ok"},
        "call_id": call_id,
        "node_id": node_id,
        "schema_version": 1,
        "governance_chain_tip": call_id,
        "samples": [sample],
    }


def ulidish(n: int) -> str:
    """A valid Crockford-base32 ULID (the controller validates every id it sees)."""
    crockford = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    val = ((int(time.time() * 1000) & ((1 << 48) - 1)) << 80) | ((n * 2654435761) & ((1 << 80) - 1))
    out = []
    for _ in range(26):
        out.append(crockford[val & 0x1F])
        val >>= 5
    return "".join(reversed(out))


def db_rows(db_path: str, sql: str, args=()) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def ram_pct(sample: dict) -> float:
    return 100.0 * sample["ram_used_bytes"] / sample["ram_total_bytes"]


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Operator fault console acceptance probe")
    ap.add_argument("--keep", action="store_true", help="keep the scratch DB/boot logs")
    ap.add_argument("--state", default=None, help="state dir (default: a temp dir)")
    ap.add_argument("--oom-seconds", type=float, default=8.0)
    ap.add_argument("--step-s", type=float, default=1.0)
    ap.add_argument("--observe-s", type=float, default=25.0,
                    help="how long beat (a) waits for the rule + decision")
    ap.add_argument("--inject-ttl", type=float, default=0.0,
                    help="injection TTL (default: long enough to outlive the decision)")
    ap.add_argument("--decision-mode", default="deterministic", choices=("deterministic", "model"),
                    help="operator decision mode (model = a real call to the model service)")
    ap.add_argument("--inference-base-url",
                    default=os.environ.get("TERMINI_INFERENCE_BASE_URL", ""),
                    help="model service for --decision-mode=model "
                         "(default: $TERMINI_INFERENCE_BASE_URL)")
    ap.add_argument("--decision-timeout-s", type=float, default=25.0)
    ap.add_argument("--guard-s", type=float, default=60.0,
                    help="TERMINI_DEMO_FAULT_GUARD_S: the remedation settle window")
    args = ap.parse_args(argv)

    state = pathlib.Path(args.state or tempfile.mkdtemp(prefix="fault-probe-"))
    state.mkdir(parents=True, exist_ok=True)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    project_id = "01J0DEM0WARM000000000000AB"
    project_token = "probe-project-token"
    bootstrap = "probe-boot-1,probe-boot-2"
    db_path = str(state / "controller.db")

    env = dict(os.environ)
    env.update({
        "TERMINI_HOST": "127.0.0.1",
        "TERMINI_PORT": str(port),
        "TERMINI_DB_PATH": db_path,
        "TERMINI_PROJECT_ID": project_id,
        "TERMINI_PROJECT_TOKEN": project_token,
        "TERMINI_BOOTSTRAP_TOKEN": bootstrap,
        "TERMINI_OPERATOR_ENABLED": "true",
        # Dry run off by default would need an LXD endpoint; the probe keeps the
        # executor hermetic by pointing it at the chaos LXD stub when one is up,
        # else dry-run. Either way the *decision* and *audit* legs are exercised.
        "TERMINI_OPERATOR_DRY_RUN": "true",
        "TERMINI_OPERATOR_POLL_INTERVAL_S": "1",
        "TERMINI_OPERATOR_COOLDOWN_S": "3",
        "TERMINI_OOM_CONSECUTIVE": "2",
        "TERMINI_OOM_CLEAR_CONSECUTIVE": "2",
        "TERMINI_OPERATOR_DECISION_MODE": args.decision_mode,
        "TERMINI_OPERATOR_DECISION_TIMEOUT_S": str(args.decision_timeout_s),
        "TERMINI_DEMO_FAULT_GUARD_S": str(args.guard_s),
    })
    if args.inference_base_url:
        env["TERMINI_INFERENCE_BASE_URL"] = args.inference_base_url
    print(f"# controller : {base} (db {db_path})")
    print(f"# project    : {project_id}")
    log_file = open(state / "controller.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.main"], cwd=str(REPO / "controller"),
        env=env, stdout=log_file, stderr=subprocess.STDOUT,
    )
    try:
        if not wait_up(base):
            print("controller did not come up; see", state / "controller.log")
            return 2

        # ---- nodes -------------------------------------------------------- #
        print("\n== nodes ==")
        nodes = {}
        for i, (host, synthetic) in enumerate((("probe-node-a", False), ("probe-node-b", True)), start=1):
            # Mint a fresh single-use bootstrap token per node: a replay of the
            # startup tokens would be refused `401 bootstrap token already used` on
            # a second run against the same --state dir (the seeder does the same).
            st, minted = call(base, "POST", "/v1/bootstrap-tokens", {"count": 1},
                              token=project_token)
            token = (minted or {}).get("tokens", [None])[0] if st in (200, 201) else f"probe-boot-{i}"
            st, reg = call(base, "POST", "/v1/nodes/register",
                           registration_body(host, synthetic=synthetic), token=token)
            assert st in (200, 201), (st, reg)
            nodes[host] = {"node_id": reg["node_id"], "token": reg["node_token"], "synthetic": synthetic}
            print(f"  registered {host}: node_id={reg['node_id']} role={reg['role']} "
                  f"synthetic={synthetic}")
        real = nodes["probe-node-a"]
        syn = nodes["probe-node-b"]

        # healthy telemetry from the "real" node, so the injector has a baseline
        seq = 100
        for _ in range(2):
            seq += 1
            st, body = call(base, "POST", f"/v1/nodes/{real['node_id']}/telemetry",
                            envelope(real["node_id"], healthy_sample(real["node_id"], project_id, seq),
                                     ulidish(seq)), token=real["token"])
            assert st == 202, (st, body)
            time.sleep(0.2)

        # ---- d. auth ------------------------------------------------------ #
        print("\n== d. the endpoint is authenticated (and not reachable from a node) ==")
        st_anon, _ = call(base, "POST", f"/v1/projects/{project_id}/demo/faults",
                          {"node_id": real["node_id"], "scenario": "oom", "target": "real",
                           "duration_s": 10})
        check("no token -> 401", st_anon == 401, f"HTTP {st_anon}")
        st_node, node_err = call(base, "POST", f"/v1/projects/{project_id}/demo/faults",
                                 {"node_id": real["node_id"], "scenario": "oom", "target": "real",
                                  "duration_s": 10}, token=real["token"])
        check("node token -> 403 (node context cannot inject)", st_node == 403, f"HTTP {st_node}")
        st_get, _ = call(base, "GET", f"/v1/projects/{project_id}/demo/faults")
        check("GET without a token -> 401", st_get == 401, f"HTTP {st_get}")

        # ---- a. inject an OOM on the chosen node -------------------------- #
        print("\n== a. inject OOM on the chosen node ==")
        # The TTL must outlive the decision: in model mode the decision step is a
        # real model call and can take the whole decision timeout, and an injection
        # whose TTL expires first would (correctly) auto-clear underneath the beat.
        ttl = args.inject_ttl or max(args.oom_seconds + 10.0, args.observe_s + 10.0)
        st, inj = call(base, "POST", f"/v1/projects/{project_id}/demo/faults",
                       {"node_id": real["node_id"], "scenario": "oom", "duration_s": ttl,
                        "target": "real"}, token=project_token)
        check("POST /demo/faults -> 201", st == 201, f"HTTP {st} {inj if st != 201 else ''}")
        if st != 201:
            return 1
        injection_id = inj["injection_id"]
        print(f"  injection_id={injection_id} rule={inj['rule_id']} ttl={inj['duration_s']}s "
              f"expires_at={inj['expires_at']} target={inj['target']}")

        # ---- c1. the duplicate-injection guard, while it IS running -------- #
        st, dup = call(base, "POST", f"/v1/projects/{project_id}/demo/faults",
                       {"node_id": real["node_id"], "scenario": "oom", "duration_s": 10,
                        "target": "real"}, token=project_token)
        reasons = [d.get("reason") for d in ((dup or {}).get("error", {}).get("details") or [])]
        check("a second injection for the same (node, rule) is refused 409 fault_already_active",
              st == 409 and "fault_already_active" in reasons, f"HTTP {st} details={reasons}")
        check("the refusal names the injection already running",
              injection_id in json.dumps(dup), f"injection {injection_id}")

        # the node's own samples must no longer be stored while the fault is open
        seq += 1
        st, body = call(base, "POST", f"/v1/nodes/{real['node_id']}/telemetry",
                        envelope(real["node_id"], healthy_sample(real["node_id"], project_id, seq),
                                 ulidish(seq)), token=real["token"])
        check("node's own sample superseded while the fault is open",
              st == 202 and body.get("accepted") == 0 and body.get("rejected") == 1,
              f"HTTP {st} {body}")

        # watch the injected series and the loop
        fired = None
        decision = None
        log_lines = []
        pts: list = []
        peak_pct = 0.0
        decision_started = time.time()
        deadline = time.time() + max(25.0, args.oom_seconds + 20.0, args.observe_s)
        while time.time() < deadline:
            time.sleep(args.step_s)
            st, series = call(base, "GET",
                              f"/v1/projects/{project_id}/nodes/{real['node_id']}/metrics"
                              f"?metrics=ram_used_bytes&resolution=raw", token=project_token)
            pts = series["series"][0]["points"] if st == 200 and series["series"] else pts
            peak_pct = max([p["v"] / (8 * GiB) * 100 for p in pts] or [0.0])
            st, ev = call(base, "GET", f"/v1/operator/events?node={real['node_id']}&limit=5",
                          token=project_token)
            st, dec = call(base, "GET", "/v1/operator/decisions?limit=5", token=project_token)
            log_lines = db_rows(db_path, "SELECT ts, node_id, severity, actor, message"
                                         " FROM operator_logs WHERE project_id=? ORDER BY log_id",
                                (project_id,))
            if ev.get("events"):
                fired = ev["events"][0]
            if dec.get("decisions"):
                decision = dec["decisions"][0]
            if fired and decision:
                break
        print(f"  injected series: {len(pts)} samples, ram% peak {peak_pct:.1f} "
              f"(last {pts[-1]['v'] / (8 * GiB) * 100:.1f})" if pts else "  injected series: none")
        check("injected series drove RAM past the OOM threshold",
              peak_pct >= 95.0, f"peak ram%={peak_pct:.1f}")
        check("the OOM rule fired", fired is not None and fired["rule_id"] == "oom-guard-01",
              f"{fired['rule_id']} value={fired['value']}" if fired else "no health event")
        check("the operator log carries 'fault injected by operator'",
              any("fault injected by operator" in (l["message"] or "") for l in log_lines),
              next((l["message"] for l in log_lines if "fault injected by operator" in (l["message"] or "")), ""))
        check("the decision step ran and recorded an action",
              decision is not None,
              f"{decision['action_kind']} source={decision.get('decision_source')} "
              f"fallback={decision.get('fallback_reason')} after "
              f"{time.time() - decision_started:.1f}s reason={decision.get('reason')}"
              if decision else "no decision row")
        check("a remediation audit was written",
              bool(db_rows(db_path, "SELECT id FROM remediation_audits WHERE node_id=?",
                           (real["node_id"],))), "audit row(s) present")

        # ---- c2. a different rule on the same node stays injectable -------- #
        print("\n== c. guards ==")
        st, rem = call(base, "POST", f"/v1/projects/{project_id}/demo/faults",
                       {"node_id": real["node_id"], "scenario": "disk-fail", "duration_s": 8,
                        "target": "real"}, token=project_token)
        check("a different rule on the same node is still injectable", st == 201,
              f"HTTP {st} (rule=disk-fail-01)")

        # ---- b. clear ----------------------------------------------------- #
        print("\n== b. clear restores the series and the rule returns below threshold ==")
        st, cleared = call(base, "DELETE", f"/v1/projects/{project_id}/demo/faults/{injection_id}",
                           token=project_token)
        check("DELETE /demo/faults/{id} -> 200", st == 200, f"HTTP {st}")
        check("clear starts the recovery burst",
              cleared.get("state") == "recovering",
              f"state={cleared.get('state')} needs {cleared.get('clear_samples_needed')} healthy samples")
        time.sleep(args.step_s * (cleared.get("clear_samples_needed", 3) + 2))
        st, lst = call(base, "GET", f"/v1/projects/{project_id}/demo/faults?state=open",
                       token=project_token)
        open_ids = [f["injection_id"] for f in lst.get("faults", [])]
        check("the injection is no longer open", injection_id not in open_ids,
              f"open now: {open_ids or 'none'}")
        st, series = call(base, "GET",
                          f"/v1/projects/{project_id}/nodes/{real['node_id']}/metrics"
                          f"?metrics=ram_used_bytes&resolution=raw", token=project_token)
        tail = series["series"][0]["points"][-3:] if series.get("series") else []
        check("the restored series is below the OOM clear threshold (90%)",
              bool(tail) and all(p["v"] / (8 * GiB) * 100 <= 90.0 for p in tail),
              "last 3 ram%: " + ", ".join(f"{p['v'] / (8 * GiB) * 100:.1f}" for p in tail))
        st, ev = call(base, "GET", f"/v1/operator/events?node={real['node_id']}&limit=20",
                      token=project_token)
        check("the rule re-armed instead of re-firing",
              len([e for e in ev.get("events", []) if e["rule_id"] == "oom-guard-01"]) == 1,
              f"{len(ev.get('events', []))} health event(s) total")
        st, again = call(base, "DELETE", f"/v1/projects/{project_id}/demo/faults/{injection_id}",
                         token=project_token)
        check("clearing again is idempotent", st == 200 and again.get("status") == "already_cleared",
              f"HTTP {st} status={again.get('status')}")

        # guard 3: the operator's remediation for oom-guard-01 just ran on this node,
        # so re-injecting that rule must be refused until the fleet has settled.
        st, recent = call(base, "POST", f"/v1/projects/{project_id}/demo/faults",
                          {"node_id": real["node_id"], "scenario": "oom", "duration_s": 8,
                           "target": "real"}, token=project_token)
        reasons = [d.get("reason") for d in ((recent or {}).get("error", {}).get("details") or [])]
        check("re-injecting the rule the operator just remediated is refused 409 "
              "remediation_in_flight", st == 409 and "remediation_in_flight" in reasons,
              f"HTTP {st} details={reasons} message={(recent or {}).get('error', {}).get('message')}")

        # ---- e. TTL ------------------------------------------------------- #
        print("\n== e. a live injection ends by itself at its TTL (never silently) ==")
        st, ttl_inj = call(base, "POST", f"/v1/projects/{project_id}/demo/faults",
                           {"node_id": syn["node_id"], "scenario": "latency", "duration_s": 5,
                            "target": "synthetic"}, token=project_token)
        check("inject on the synthetic node -> 201", st == 201, f"HTTP {st}")
        ttl_id = ttl_inj["injection_id"]
        time.sleep(6)
        time.sleep(args.step_s * 4)
        st, lst = call(base, "GET", f"/v1/projects/{project_id}/demo/faults", token=project_token)
        row = next((f for f in lst.get("faults", []) if f["injection_id"] == ttl_id), None)
        check("the TTL injection ended without an operator",
              row is not None and row["state"] == "cleared" and row["clear_reason"] == "ttl_expired",
              f"state={row and row['state']} reason={row and row['clear_reason']}")
        log_lines = db_rows(db_path, "SELECT ts, node_id, severity, actor, message"
                                     " FROM operator_logs WHERE project_id=? ORDER BY log_id",
                            (project_id,))
        check("the TTL expiry is recorded in the operator log",
              any("TTL expired" in (l["message"] or "") for l in log_lines),
              next((l["message"] for l in log_lines if "TTL expired" in (l["message"] or "")), ""))

        # ---- f. a restart cannot leave an injection silently live ---------- #
        print("\n== f. a controller restart sweeps an injection it can no longer drive ==")
        st, orphan = call(base, "POST", f"/v1/projects/{project_id}/demo/faults",
                          {"node_id": syn["node_id"], "scenario": "disk-degrade",
                           "duration_s": 120, "target": "synthetic"}, token=project_token)
        orphan_id = (orphan or {}).get("injection_id")
        check("injection running before the restart", st == 201 and bool(orphan_id), f"HTTP {st}")
        proc.terminate()
        proc.wait(timeout=15)
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.main"], cwd=str(REPO / "controller"),
            env=env, stdout=log_file, stderr=subprocess.STDOUT,
        )
        if not wait_up(base):
            check("controller restarted", False, "did not come back up")
        else:
            st, lst = call(base, "GET", f"/v1/projects/{project_id}/demo/faults",
                           token=project_token)
            row = next((f for f in lst.get("faults", []) if f["injection_id"] == orphan_id), None)
            check("the orphaned injection is reported expired, not active",
                  row is not None and row["state"] == "expired"
                  and row["cleared_by"] == "controller_restart",
                  f"state={row and row['state']} cleared_by={row and row['cleared_by']}")
            log_lines = db_rows(db_path, "SELECT ts, node_id, severity, actor, message"
                                         " FROM operator_logs WHERE project_id=? ORDER BY log_id",
                                (project_id,))
            check("the sweep is recorded in the operator log",
                  any("controller restarted while" in (l["message"] or "") for l in log_lines),
                  next((l["message"] for l in log_lines if "controller restarted while" in (l["message"] or "")), ""))

        # ---- summary ------------------------------------------------------ #
        print("\n== operator log (what the client dashboard shows) ==")
        for l in log_lines:
            print(f"  {l['ts']} [{l['severity']}] ({l['actor']}) {l['message']}")
        print("\n== decisions ==")
        for d in call(base, "GET", "/v1/operator/decisions?limit=10", token=project_token)[1]["decisions"]:
            print(f"  {d.get('decided_at')} {d['rule_id']} -> {d['action_kind']} "
                  f"source={d.get('decision_source')} conf={d.get('confidence')} "
                  f"fallback={d.get('fallback_reason')} {d.get('reason')}")
        print(f"\n# state dir: {state}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()

    print(f"\n{'ALL BEATS PASSED' if not FAILURES else 'FAILURES: ' + '; '.join(FAILURES)}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
