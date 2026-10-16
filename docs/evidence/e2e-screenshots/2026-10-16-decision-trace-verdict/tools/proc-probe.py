#!/usr/bin/env python3
"""Probe: does a /proc/<pid>/cmdline-shaped C2 VALUE (which scan_c2's signatures
do NOT cover) leak through the trace when it is the ONLY C2-shaped content?
This isolates the card's third example from the email/card that trip the
externalModelCall assertion, so we see whether the trace inherits scan_c2's
value-signature gap.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

REPO = pathlib.Path(os.environ["REPO"]).resolve()
sys.path.insert(0, str(REPO / "controller"))
sys.path.insert(0, str(REPO))

from app import db, store, ulid  # noqa: E402
from app.operator import decision  # noqa: E402
from app.operator.models import HealthEvent  # noqa: E402

PROC = "/proc/31337/cmdline"
VM_ID = "01M4349RPXWDC52ZDAVMPCMJSH"
NODE_ID = "01M4349RPTT29RNTC6VBCXQ0RC"
PROJECT_ID = "01M4349RPSZFTB60AVJEG0ZZY9"


def make_event():
    return HealthEvent(
        event_id="01M4349RQ09HMAX6RC9YPYQH4W", node_id=NODE_ID,
        rule_id="oom-guard-01", type="oom", metric="ram_used_bytes", value=95.0,
        threshold=">= 95% of ram_total_bytes for 2 consecutive samples",
        fired_at="2026-10-16T08:00:00.000Z",
        breach_started_at="2026-10-16T07:59:58.000Z",
        evidence=[{"seq": 2, "ram_used_bytes": 32641751449}],
    )


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="termini-procprobe-")
    db_path = os.path.join(tmp, "p.db")
    db.migrate(db_path)
    with db.session(db_path) as conn:
        conn.execute(
            "INSERT INTO projects (id, name, project_token_hash, max_inference_nodes, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (PROJECT_ID, "p", "hash", 1, store.now_iso()))
        conn.execute(
            "INSERT INTO nodes (id, project_id, registration_id, machine_id,"
            " hostname_hash, role, node_token_hash, hardware_json, os_json,"
            " lxd_json, role_hint, installer_version, status, last_seen_at,"
            " registered_at, source_ip)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (NODE_ID, PROJECT_ID, "reg-1", None, "5f2a9c1e7b4d4a1f", "compute",
             "tokhash", "{}", None, None, "auto", "0.1.0", "online",
             store.now_iso(), store.now_iso(), None))

    planted = {
        "seq": 2, "ts": "2026-10-16T08:00:02.000Z",
        "ram_total_bytes": 34_359_738_368, "ram_used_bytes": 34_000_000_000,
        "kernel_version": f"Linux 6.8 {PROC}",
        "lxd_instances": [{"vm_id": VM_ID, "state": "running",
                            "ram_used_bytes": 10_000_000_000, "ram_limit_bytes": 16_000_000_000}],
    }
    with db.session(db_path) as conn:
        store.insert_samples(conn, NODE_ID, ulid.new_ulid(), [
            {"seq": 1, "ts": "2026-10-16T08:00:01.000Z", "ram_total_bytes": 34_359_738_368,
             "ram_used_bytes": 20_000_000_000, "lxd_instances": planted["lxd_instances"]},
            planted,
        ])
        evidence = store.recent_samples(conn, NODE_ID, 10)

    latest = evidence[-1]
    ctx = decision.DecisionContext(
        event=make_event(), state=dict(latest), evidence=evidence,
        instances=list(latest.get("lxd_instances", [])),
        instance_names={VM_ID: "agent-c1"}, default_instance_name="agent",
        allowed_actions=list(decision.ACTION_IDS), project_id=PROJECT_ID)
    det = decision.DeterministicDecisionProvider(
        playbooks={}, instance_names={VM_ID: "agent-c1"}, default_instance_name="agent")

    from termini.decision.types import DecisionResponse

    def stub_decide(_inp):
        # A compromised/prompt-injected model that quotes the telemetry it saw.
        return DecisionResponse(
            action_id="instance.restart", confidence=0.9,
            rationale="restart heaviest", params={"instance_name": "heaviest"},
            raw=json.dumps({"rationale": f"restart; saw kernel {PROC}"}))

    provider = decision.ModelDecisionProvider(
        deterministic=det, decide_fn=stub_decide,
        instance_names={VM_ID: "agent-c1"}, default_instance_name="agent")
    result = provider.decide(make_event(), ctx)
    trace_json = json.dumps(result.trace.to_dict(), default=str)
    print(f"result.source={result.source!r}")
    print(f"trace contains {PROC!r}: {PROC in trace_json}")
    reply = next((s for s in result.trace.steps if s['step'] == 'reply_received'), None)
    if reply:
        print(f"reply step detail: {reply['detail']!r} state={reply['state']!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
