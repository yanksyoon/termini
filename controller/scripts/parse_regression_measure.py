#!/usr/bin/env python3
"""Measure the operator decision parse path over 20 synthetic faults.

The live demo (2026-10-17) had 2 of 5 injected faults fall back with
``parse_error``: Gemini answered, the adapter could not parse the reply, and the
deterministic playbook decided instead. This script replays that path — the
REAL ``termini.decision`` adapter + ``ModelDecisionProvider`` guardrail + fallback
— over 20 synthetic health events, feeding each one a realistic model reply in
the shapes that used to fail parsing (a fenced ```json block, leading/trailing
prose, a multiline object). It tallies ``source`` and ``fallback_reason`` and
prints the 20-row table the PR carries. Target: 0 parse_error in 20.

The model transport is a stub client injected through the ``decide_fn`` seam
(the same seam the unit tests and the demo's ``ModelDecisionProvider`` use), so
the adapter's real ``render``/``parse`` and the controller's real guardrail run
unchanged — only the HTTP hop is a stub. No LXD, no network.

    python3 controller/scripts/parse_regression_measure.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "controller"))
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO))

from app.operator import decision  # noqa: E402
from app.operator.models import HealthEvent  # noqa: E402
from termini.decision import adapter  # noqa: E402

VM_ID = "01M4349RPXWDC52ZDAVMPCMJSH"
NODE_ID = "01M4349RPTT29RNTC6VBCXQ0RC"
INSTANCE_NAME = "agent-c1"

# A minimal playbook map so a fallback (which the model must never hide) can
# still produce the deterministic action with a NAMED reason.
PLAYBOOKS = {
    "oom": {
        "type": "oom", "rule_id": "oom-guard-01",
        "action": {"kind": "instance.restart", "target": "heaviest",
                   "params": {"force": True, "timeout_s": 30}},
        "reason": "oom", "description": "oom",
    },
    "disk-degrade": {
        "type": "disk_degrade", "rule_id": "disk-degrade-01",
        "action": {"kind": "instance.stop", "target": "heaviest",
                   "params": {"force": True}},
        "reason": "disk degrade", "description": "disk degrade",
    },
    "disk-fail": {
        "type": "disk_fail", "rule_id": "disk-fail-01",
        "action": {"kind": "instance.stop", "target": "heaviest",
                   "params": {"force": True}},
        "reason": "disk fail", "description": "disk fail",
    },
    "latency": {
        "type": "latency", "rule_id": "latency-01",
        "action": {"kind": "instance.restart", "target": "heaviest",
                   "params": {"force": True, "timeout_s": 30}},
        "reason": "latency", "description": "latency",
    },
}

# Which action each scenario's *valid* model reply should choose.
SCENARIO_ACTION = {
    "oom": "instance.restart",
    "disk-degrade": "instance.stop",
    "disk-fail": "instance.stop",
    "latency": "instance.restart",
}

SCENARIO_METRIC = {
    "oom": "ram_used_bytes",
    "disk-degrade": "disk.write_latency_ms",
    "disk-fail": "disk.mount_ro",
    "latency": "net.latency_ms",
}


def make_event(scenario: str, i: int) -> HealthEvent:
    return HealthEvent(
        event_id=f"01M5588N1CWHZMZ4KAF5PQAM{i:02d}",
        node_id=NODE_ID,
        rule_id=PLAYBOOKS[scenario]["rule_id"],
        type=PLAYBOOKS[scenario]["type"],
        metric=SCENARIO_METRIC[scenario],
        value=95.0 if scenario == "oom" else 80.0,
        threshold="threshold",
        fired_at="2026-10-17T15:38:00.000Z",
    )


def make_context(event: HealthEvent) -> decision.DecisionContext:
    insts = [{"vm_id": VM_ID, "state": "running", "ram_used_bytes": 10_000_000_000}]
    return decision.DecisionContext(
        event=event,
        state={"seq": 2, "ram_used_bytes": 32_000_000_000},
        evidence=[{"seq": 1, "lxd_instances": insts}],
        instances=insts,
        instance_names={VM_ID: INSTANCE_NAME},
        default_instance_name="agent",
        allowed_actions=list(decision.ACTION_IDS),
        project_id="01M4349RPSZFTB60AVJEG0ZZY9",
    )


def decision_dict(scenario: str, request_id: str, *, confidence: float,
                  action_id: str, instance_name: str) -> dict:
    action = SCENARIO_ACTION[scenario]
    params: dict = {"instance_name": instance_name, "force": True}
    if action == "instance.restart":
        params["timeout_s"] = 30
    return {
        "schema_version": 1,
        "request_id": request_id,
        "action_id": action_id,
        "params": params,
        "confidence": confidence,
        "rationale": (
            f"{SCENARIO_METRIC[scenario]} breached its threshold on the node; "
            f"{action} on {instance_name} is the least destructive remediation."
        ),
        "incident_title": f"{scenario} on node {NODE_ID}",
        "explanation": (
            f"The evidence shows {SCENARIO_METRIC[scenario]} outside its healthy "
            "band, so the incident is real and the chosen action removes the load."
        ),
        "evidence": [{"field": SCENARIO_METRIC[scenario], "value": 80.0, "seq": 1}],
        "actions": [{"action_kind": action, "target": instance_name,
                     "note": "remove the load"}],
    }


def wrap(shape: str, d: dict) -> str:
    """Wrap a decision dict in the reply shape a model actually emits."""
    compact = json.dumps(d)
    pretty = json.dumps(d, indent=2)
    if shape == "clean":
        return compact
    if shape == "fenced":
        return "```json\n" + pretty + "\n```"
    if shape == "prose_fenced":
        return (
            "Here is the remediation decision for this fault.\n\n"
            "```json\n" + pretty + "\n```\n\nI hope that helps."
        )
    if shape == "leading_prose":
        return "The best action, given the evidence, is:\n" + compact
    if shape == "multiline_trailing":
        return pretty + "\n\nLet me know if you need anything else."
    raise AssertionError(shape)


def build_runs() -> list[dict]:
    """The 20 synthetic faults: scenario, reply shape, and the expected outcome.

    Runs 1..16 are VALID decisions wrapped in the shapes that used to fail
    parsing — after the fix each must decide ``model``. Runs 17..20 are valid
    JSON that the guardrail (not the parser) rejects, to prove a fallback still
    names its reason (low_confidence / invalid_target) and never parse_error.
    """
    scenarios = ["oom", "disk-degrade", "disk-fail", "latency"]
    shapes = ["clean", "fenced", "prose_fenced", "leading_prose", "multiline_trailing"]
    runs: list[dict] = []
    for i in range(16):
        scenario = scenarios[i % 4]
        shape = shapes[i % 5]
        rid = f"01M5588N1CWHZMZ4KAF5PQAM{i:02d}"
        runs.append({
            "scenario": scenario, "shape": shape, "request_id": rid,
            "reply": wrap(shape, decision_dict(
                scenario, rid, confidence=0.85, action_id=SCENARIO_ACTION[scenario],
                instance_name="heaviest",
            )),
        })
    # Two low-confidence decisions -> guardrail G6 fallback (named reason).
    for i, scenario in enumerate(["oom", "disk-degrade"], start=16):
        rid = f"01M5588N1CWHZMZ4KAF5PQAM{i:02d}"
        runs.append({
            "scenario": scenario, "shape": "fenced", "request_id": rid,
            "reply": wrap("fenced", decision_dict(
                scenario, rid, confidence=0.1, action_id=SCENARIO_ACTION[scenario],
                instance_name="heaviest",
            )),
        })
    # Two unresolvable targets -> guardrail G5 fallback (named reason).
    for i, scenario in enumerate(["disk-fail", "latency"], start=18):
        rid = f"01M5588N1CWHZMZ4KAF5PQAM{i:02d}"
        runs.append({
            "scenario": scenario, "shape": "prose_fenced", "request_id": rid,
            "reply": wrap("prose_fenced", decision_dict(
                scenario, rid, confidence=0.9, action_id=SCENARIO_ACTION[scenario],
                instance_name="nonexistent-vm",
            )),
        })
    return runs


def main() -> int:
    det = decision.DeterministicDecisionProvider(
        playbooks=PLAYBOOKS, instance_names={VM_ID: INSTANCE_NAME},
        default_instance_name="agent",
    )
    runs = build_runs()

    counts: dict[str, int] = {"model": 0, "fallback": 0}
    fallback_reasons: dict[str, int] = {}
    rows: list[dict] = []

    for i, run in enumerate(runs, start=1):
        event = make_event(run["scenario"], i)
        ctx = make_context(event)
        reply = run["reply"]

        def mock_client(messages, request_id=None, *, _reply=reply):
            # Record the raw reply is passed through the REAL adapter parse path.
            return _reply

        # decide_fn is the documented transport seam; the REAL adapter.decide
        # (render -> call -> parse) runs, then the REAL guardrail + fallback.
        provider = decision.ModelDecisionProvider(
            deterministic=det, decide_fn=lambda inp: adapter.decide(inp, client=mock_client),
            instance_names={VM_ID: INSTANCE_NAME}, default_instance_name="agent",
        )
        result = provider.decide(event, ctx)

        source = result.source
        reason = result.fallback_reason
        counts[source] = counts.get(source, 0) + 1
        if source == "fallback" and reason:
            fallback_reasons[reason] = fallback_reasons.get(reason, 0) + 1
        rows.append({
            "run": i, "scenario": run["scenario"], "shape": run["shape"],
            "source": source, "fallback_reason": reason,
        })

    print("run | scenario      | reply shape        | source    | fallback_reason")
    print("----+---------------+--------------------+-----------+----------------")
    for r in rows:
        reason = r["fallback_reason"] or "-"
        print(f"{r['run']:3d} | {r['scenario']:<13} | {r['shape']:<18} | "
              f"{r['source']:<9} | {reason}")

    parse_errors = fallback_reasons.get("parse_error", 0)
    print("\ncounts by outcome:")
    print(f"  decided by model : {counts.get('model', 0)}")
    print(f"  fallback         : {counts.get('fallback', 0)}")
    if fallback_reasons:
        print("  fallback reasons : " + ", ".join(
            f"{k}={v}" for k, v in sorted(fallback_reasons.items())))
    else:
        print("  fallback reasons : (none)")
    print(f"  parse_error      : {parse_errors}")

    # Every fallback must carry a §10.1 reason; the target is zero parse errors.
    unnamed = [r for r in rows if r["source"] == "fallback" and not r["fallback_reason"]]
    assert not unnamed, f"fallback row with no reason: {unnamed}"
    assert parse_errors == 0, f"parse_error in {parse_errors} of 20 runs"
    print("\nPASS: 0 parse_error, every fallback named its reason")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
