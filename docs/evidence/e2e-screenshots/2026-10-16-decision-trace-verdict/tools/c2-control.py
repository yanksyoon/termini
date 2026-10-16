#!/usr/bin/env python3
"""Adversarial C2 control for the §4.3 decision trace (independent instrument).

Not the implementer's test. This plants a C2-shaped value into the incident's
own telemetry (the telemetry_samples store, the same table the node's telemetry
writes), then runs the COMMITTED decision path (ModelDecisionProvider + the
externalModelCall class assertion + the deterministic fallback) and proves the
planted value appears in NO trace field, NO incident field, and NO committed
capture -- while being plainly visible in the raw store (positive control).

Then it falsifies the scanner: the same C2 value placed in a field the trace
DOES carry (the raw model reply / a step detail) IS caught, so a zero-hit on the
main path is meaningful, not the result of a blind scanner.
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
from app.operator import decision, detect  # noqa: E402
from app.operator import store as opstore  # noqa: E402
from app.operator.models import HealthEvent  # noqa: E402
from app import governance  # noqa: E402
from app.operator import playbook  # noqa: E402

# ---- the C2 markers I am planting (unambiguous scan_c2 signatures) ----
EMAIL = "adversary-plant-9f3c@example.com"
CARD = "4111111111111111"   # 16-digit PAN shape
PROC = "/proc/31337/cmdline"  # C2-in-spirit (a process path); NOT a scan_c2 signature

ALL_MARKERS = [EMAIL, CARD, PROC]

VM_ID = "01M4349RPXWDC52ZDAVMPCMJSH"
NODE_ID = "01M4349RPTT29RNTC6VBCXQ0RC"
PROJECT_ID = "01M4349RPSZFTB60AVJEG0ZZY9"


def make_event():
    return HealthEvent(
        event_id="01M4349RQ09HMAX6RC9YPYQH4W",
        node_id=NODE_ID, rule_id="oom-guard-01", type="oom",
        metric="ram_used_bytes", value=95.0,
        threshold=">= 95% of ram_total_bytes for 2 consecutive samples",
        fired_at="2026-10-16T08:00:00.000Z",
        breach_started_at="2026-10-16T07:59:58.000Z",
        evidence=[{"seq": 2, "ram_used_bytes": 32641751449}],
    )


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="termini-c2control-")
    db_path = os.path.join(tmp, "c2.db")
    db.migrate(db_path)
    print(f"DB migrated at {db_path}")

    with db.session(db_path) as conn:
        conn.execute(
            "INSERT INTO projects (id, name, project_token_hash, max_inference_nodes, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (PROJECT_ID, "c2", "hash", 1, store.now_iso()))
        conn.execute(
            "INSERT INTO nodes (id, project_id, registration_id, machine_id,"
            " hostname_hash, role, node_token_hash, hardware_json, os_json,"
            " lxd_json, role_hint, installer_version, status, last_seen_at,"
            " registered_at, source_ip)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (NODE_ID, PROJECT_ID, "reg-1", None, "5f2a9c1e7b4d4a1f", "compute",
             "tokhash", "{}", None, None, "auto", "0.1.0", "online",
             store.now_iso(), store.now_iso(), None))

    # ---- PLANT: a telemetry sample carrying the C2 markers in kernel_version
    # (a free-form C0 str field). store.insert_samples does NOT validate payload
    # content -- exactly the "compromised redactor wrote C2 into the store" case.
    planted_sample = {
        "seq": 2, "ts": "2026-10-16T08:00:02.000Z",
        "ram_total_bytes": 34_359_738_368,
        "ram_used_bytes": 34_000_000_000,
        "kernel_version": f"Linux 6.8 planted {EMAIL} {CARD} {PROC}",
        "lxd_instances": [
            {"vm_id": VM_ID, "state": "running", "ram_used_bytes": 10_000_000_000,
             "ram_limit_bytes": 16_000_000_000},
        ],
    }
    with db.session(db_path) as conn:
        ins, _ = store.insert_samples(conn, NODE_ID, ulid.new_ulid(), [
            {"seq": 1, "ts": "2026-10-16T08:00:01.000Z",
             "ram_total_bytes": 34_359_738_368, "ram_used_bytes": 20_000_000_000,
             "lxd_instances": planted_sample["lxd_instances"]},
            planted_sample,
        ])
        print(f"planted samples inserted={ins}")

    # ---- POSITIVE CONTROL: the value IS in the raw store ----
    with db.session(db_path) as conn:
        stored = store.recent_samples(conn, NODE_ID, 10)
    raw_blob = json.dumps(stored, default=str)
    for m in (EMAIL, CARD, PROC):
        print(f"POSITIVE CONTROL raw-store contains {m!r}: {m in raw_blob}")

    # ---- run the COMMITTED decision path over that evidence ----
    evidence = list(stored)
    latest = evidence[-1]
    ctx = decision.DecisionContext(
        event=make_event(), state=dict(latest), evidence=evidence,
        instances=list(latest.get("lxd_instances", [])),
        instance_names={VM_ID: "agent-c1"}, default_instance_name="agent",
        allowed_actions=list(decision.ACTION_IDS), project_id=PROJECT_ID,
    )

    det = decision.DeterministicDecisionProvider(
        playbooks={}, instance_names={VM_ID: "agent-c1"}, default_instance_name="agent")

    called = {"n": 0}

    def stub_decide(_inp):
        # If the model were reached, it would quote the planted telemetry in its
        # reply (what a prompt-injected/compromised model does). This must never
        # run when C2 is present, because externalModelCall fails closed first.
        called["n"] += 1
        from termini.decision.types import DecisionResponse
        return DecisionResponse(
            action_id="instance.restart", confidence=0.9,
            rationale="restart heaviest", params={"instance_name": "heaviest"},
            raw=json.dumps({"note": f"leaked {EMAIL} {CARD} {PROC}"}))

    provider = decision.ModelDecisionProvider(
        deterministic=det, decide_fn=stub_decide,
        instance_names={VM_ID: "agent-c1"}, default_instance_name="agent")

    result = provider.decide(make_event(), ctx)
    print(f"decide_fn calls (must be 0 if C2 blocked pre-model): {called['n']}")
    print(f"result.source={result.source!r} fallback_reason={result.fallback_reason!r}")

    trace = result.trace
    trace_json = json.dumps(trace.to_dict(), default=str)
    incident_json = json.dumps(result.incident, default=str)

    for m in (EMAIL, CARD, PROC):
        in_trace = m in trace_json
        in_incident = m in incident_json
        print(f"NEGATIVE CONTROL {m!r}: in_trace={in_trace} in_incident={in_incident} "
              f"(both must be False)")

    # ---- also scan the trace with the controller's own scanner ----
    c2_hits = governance.scan_c2(trace.to_dict())
    print(f"scan_c2(trace) hits={c2_hits!r} (must be empty)")
    print(f"decided_by={trace.decided_by!r} (must be 'fallback')")
    for s in trace.steps:
        print(f"  step {s['step']:<16} state={s['state']:<9} detail={s['detail'][:90]!r}")

    # ---- FALSIFICATION: put the SAME C2 value in a field the trace DOES carry ----
    print("\n--- falsification control (the scanner is NOT blind) ---")
    print(f"scan_c2 on the planted email literal: {governance.scan_c2(EMAIL)!r}")
    print(f"scan_c2 on the planted card literal: {governance.scan_c2(CARD)!r}")
    t2 = decision.DecisionTrace(decided_by="model")
    rep = t2.add_reply("model reply received", f'{{"leak": "{EMAIL}"}}')
    print(f"reply step carrying {EMAIL!r} -> detail={rep['detail']!r} state={rep['state']!r} (want [dropped: C2] / rejected)")
    drops = [d for d in t2.drops]
    print(f"  drops recorded: {drops!r}")
    t3 = decision.DecisionTrace(decided_by="model")
    dropped_step = t3.add("gates", "gate", f"mail {EMAIL} now")
    print(f"step detail carrying {EMAIL!r} -> add() returned {dropped_step!r} (want None = dropped)")
    print(f"  step dropped recorded: {t3.drops!r}")

    ok = (
        called["n"] == 0
        and result.source == "fallback"
        and all(m not in trace_json for m in (EMAIL, CARD))
        and all(m not in incident_json for m in (EMAIL, CARD))
        and governance.scan_c2(trace.to_dict()) == []
        and rep["detail"] == "[dropped: C2]" and rep["state"] == "rejected"
        and dropped_step is None
        and EMAIL in raw_blob and CARD in raw_blob
    )
    print(f"\nOVERALL: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
