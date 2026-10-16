#!/usr/bin/env python3
"""Offline captured pair for the metric-expansion diagnosis (t_922b3268).

WHAT IS REAL HERE
  * a real `python3 -m app.main` controller process (own temp DB, migrated);
  * a real node registration + three real telemetry samples carrying the
    §4.12–§4.14 groups AND breaching the oom-guard-01 threshold, posted through
    `POST /v1/nodes/{nid}/telemetry` (the same store path a node uses);
  * the real OperatorLoop poll -> detect -> decide -> guardrail G1–G9 ->
    persistence -> GET /v1/operator/decisions path, including the §3.2 derived
    deltas, the §7.2 C0 assertion and the §4.3 decision trace;
  * a real LXD REST call to `chaos/lxd_stub.py`.

WHAT IS A STUB (stated plainly)
  * the MODEL ENDPOINT only: `tools/stub_model.py` answers
    POST /v1/chat/completions. Its answer is derived from the request the
    controller sent, and every request it received is logged verbatim.

Run:  python3 docs/evidence/2026-10-16-metric-expansion/tools/run_metric_diagnosis.py <out-dir>
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from chaos import node_sim  # noqa: E402

PROJECT_ID = "01J0DEM0WARM000000000000AB"
PROJECT_TOKEN = "evidence-project-token"
BOOTSTRAP_TOKEN = "ev-boot-1"
RAM_TOTAL = 68_719_476_736
VM_ID = "01M4349RPXWDC52ZDAVMPCMJSH"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(method, url, body=None, token=None, timeout=15.0):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "X-Termini-Version": "1"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode(errors="replace")}


def metric_sample(seq: int, oom_kill: int, pgmajfault: int, retransmit: int,
                  memory_psi: float, ram_used: int) -> dict:
    """One telemetry sample carrying the §4.12–§4.14 groups, breaching oom."""
    return {
        "seq": seq,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "uptime_sec": 3600 + seq,
        "cpu_util_pct": 88.0,
        "cpu_core_count": 8,
        "ram_total_bytes": RAM_TOTAL,
        "ram_used_bytes": ram_used,
        "swap_used_bytes": 0,
        "load_avg_1m": 1.42,
        "cpu_temp_celsius": 51.0,
        "os_name": "Ubuntu",
        "os_version": "24.04",
        "kernel_version": "6.8.0-142-generic",
        "lxd_cluster_role": "member",
        "hostname": "051375546db9782e",
        "ip_local": "10.201.0.0/24",
        "mac_address": "70675b0d86c14be2",
        "project_id": PROJECT_ID,
        "mem_available_bytes": 2_000_000_000 - seq * 100_000_000,
        "oom_kill_count": oom_kill,
        "pgmajfault_count": pgmajfault,
        "tcp_retransmit_segs": retransmit,
        "conntrack_count": 457,
        "conntrack_max": 262_144,
        "pressure": {
            "cpu_some_avg10": 12.3,
            "io_some_avg10": 0.0,
            "memory_some_avg10": memory_psi,
            "memory_full_avg10": 0.0,
        },
        "disk": [{"path": "/dev/sda", "total_bytes": 274_877_906_944,
                  "free_bytes": 200_000_000_000, "write_latency_ms": 3.0,
                  "mount_ro": False, "io_ticks_ms": 62_361_848 + seq,
                  "io_in_flight": 0, "inode_used": 1_129_359,
                  "inode_total": 12_976_128}],
        "net": [{"iface": "eth0", "rx_bytes": 1, "tx_bytes": 1, "latency_ms": 0.8,
                 "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0}],
        "lxd_instances": [{"vm_id": VM_ID, "state": "running",
                           "ram_used_bytes": 10_000_000_000,
                           "ram_limit_bytes": 16_000_000_000,
                           "disk_used_bytes": 1_000_000_000,
                           "oom_kill_count": 0, "pgmajfault_count": 30_730 + seq}],
        "services": [{"name": "termini-bootstrap", "status": "running"},
                     {"name": "lxd", "status": "running"}],
    }


def main(argv=None) -> int:
    out_dir = pathlib.Path(argv[1] if argv and len(argv) > 1 else ".")
    out_dir = out_dir.resolve()
    live = out_dir / "live"
    live.mkdir(parents=True, exist_ok=True)

    vm_id = node_sim.common.new_ulid()
    cport, stubport, lxdport = free_port(), free_port(), free_port()
    base = f"http://127.0.0.1:{cport}"
    # The controller's DB + logs are scratch, not evidence: keep them out of the
    # evidence dir entirely (tempfile, removed on the next TMPDIR prune).
    work = pathlib.Path(tempfile.mkdtemp(prefix="termini-metric-"))
    db_path = work / "controller.db"

    procs = []

    def start(cmd, cwd, log_path):
        fh = open(log_path, "w")
        p = subprocess.Popen(cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
                             env=os.environ.copy())
        procs.append((p, fh))
        return p

    try:
        # 1. stub model + stub LXD.
        start([sys.executable,
               str(REPO / "docs/evidence/e2e-screenshots/2026-10-16-incident-card/tools/stub_model.py"),
               "--port", str(stubport), "--log", str(live / "stub-model-requests.jsonl")],
              cwd=str(REPO), log_path=str(work / "stub.log"))
        start([sys.executable, str(REPO / "chaos/lxd_stub.py"),
               "--port", str(lxdport), "--log", str(live / "lxd-commands.jsonl")],
              cwd=str(REPO), log_path=str(work / "lxd.log"))

        # 2. controller (model mode).
        env = {
            "TERMINI_HOST": "127.0.0.1", "TERMINI_PORT": str(cport),
            "TERMINI_DB_PATH": str(db_path),
            "TERMINI_BOOTSTRAP_TOKEN": BOOTSTRAP_TOKEN,
            "TERMINI_PROJECT_ID": PROJECT_ID,
            "TERMINI_PROJECT_TOKEN": PROJECT_TOKEN,
            "TERMINI_MIN_REDACTOR_VERSION": "3",
            "TERMINI_OPERATOR_ENABLED": "true",
            "TERMINI_OPERATOR_POLL_INTERVAL_S": "0.5",
            "TERMINI_OPERATOR_DECISION_MODE": "model",
            "TERMINI_INFERENCE_BASE_URL": f"http://127.0.0.1:{stubport}",
            "TERMINI_OPERATOR_DECISION_TIMEOUT_S": "8",
            "TERMINI_LXD_BASE_URL": f"http://127.0.0.1:{lxdport}",
            "TERMINI_LXD_DEFAULT_INSTANCE_NAME": "agent-c1",
            "TERMINI_LXD_INSTANCE_NAMES": json.dumps({vm_id: "agent-c1"}),
            "TERMINI_OPERATOR_DRY_RUN": "false",
            "TERMINI_LXD_AUTO_PROVISION": "false",
            "PYTHONUNBUFFERED": "1",
        }
        controller = subprocess.Popen(
            [sys.executable, "-m", "app.main"], cwd=str(REPO / "controller"),
            stdout=open(str(work / "controller.log"), "w"),
            stderr=subprocess.STDOUT, env={**os.environ, **env})
        procs.append((controller, None))

        # readiness: project-token GET -> 200, and the process is still alive.
        ready = False
        for _ in range(200):
            if controller.poll() is not None:
                break
            try:
                code, _ = http("GET", f"{base}/v1/projects/{PROJECT_ID}/nodes",
                               token=PROJECT_TOKEN, timeout=2.0)
                if code == 200:
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(0.3)
        if not ready:
            print("controller did not come up", file=sys.stderr)
            return 1

        # 3. register one node.
        status, reg = http("POST", f"{base}/v1/nodes/register",
                           node_sim.build_registration("metric-node-a", gpu=False),
                           BOOTSTRAP_TOKEN)
        if status not in (200, 201) or not isinstance(reg, dict) or not reg.get("node_id"):
            print(f"registration failed: HTTP {status} {reg}", file=sys.stderr)
            return 1
        node_id, node_token = reg["node_id"], reg.get("node_token")
        if not node_token:
            print("registration returned no node_token", file=sys.stderr)
            return 1

        # 4. three breaching samples carrying the new groups.
        series = [
            (1, 37, 2_670_694, 297_277, 91.7, 66_000_000_000),
            (2, 39, 2_671_000, 297_295, 92.1, 66_200_000_000),
            (3, 40, 2_671_500, 297_300, 93.0, 66_500_000_000),
        ]
        for seq, oom, pgm, ret, psi, used in series:
            status, body = http(
                "POST", f"{base}/v1/nodes/{node_id}/telemetry",
                node_sim.build_batch(node_id, metric_sample(seq, oom, pgm, ret, psi, used)),
                node_token)
            if status not in (200, 202):
                print(f"telemetry seq {seq} rejected: HTTP {status} {body}", file=sys.stderr)
                return 1
            time.sleep(0.2)

        # 5. wait for a model decision (the operator loop polls every 0.5 s).
        decision = None
        payload = {}
        for _ in range(240):
            status, payload = http(
                "GET", f"{base}/v1/operator/decisions?limit=50", token=PROJECT_TOKEN)
            if status == 200:
                for row in payload.get("decisions") or []:
                    if row.get("incident_generated") == "model":
                        decision = row
                        break
            if decision is not None:
                break
            time.sleep(0.5)
        if decision is None:
            print("no model decision produced", file=sys.stderr)
            return 1

        (live / "decisions.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        (live / "decision-row.json").write_text(json.dumps(decision, indent=2, sort_keys=True) + "\n")
        # the persisted incident record + trace, verbatim from the API.
        (live / "incident-record.json").write_text(json.dumps({
            "incident_title": decision.get("incident_title"),
            "incident_explanation": decision.get("incident_explanation"),
            "incident_evidence": decision.get("incident_evidence"),
            "incident_actions": decision.get("incident_actions"),
            "incident_generated": decision.get("incident_generated"),
        }, indent=2, sort_keys=True) + "\n")
        (live / "decision-trace.json").write_text(json.dumps(
            decision.get("decision_trace"), indent=2, sort_keys=True) + "\n")

        print(json.dumps({
            "node_id": node_id, "vm_id": vm_id,
            "decision_id": decision.get("decision_id") or decision.get("id"),
            "decision_source": decision.get("decision_source"),
            "incident_generated": decision.get("incident_generated"),
            "incident_evidence_fields": [e.get("field") for e in decision.get("incident_evidence") or []],
            "trace_steps": [s.get("step") for s in (decision.get("decision_trace") or {}).get("steps", [])],
            "out": str(live),
        }, indent=2, sort_keys=True))
        return 0
    finally:
        for p, fh in reversed(procs):
            try:
                p.terminate()
            except Exception:
                pass
        for p, fh in procs:
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
        for p, _ in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
