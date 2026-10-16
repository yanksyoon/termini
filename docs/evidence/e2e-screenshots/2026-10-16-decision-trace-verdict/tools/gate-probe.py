#!/usr/bin/env python3
"""Independent probe: a gate-rejected fallback trace carries its reason AND the
rejecting gate (G6 low-confidence), distinct from the live transport-failure
fallback (inference_unavailable, no rejecting gate)."""
import os, pathlib, sys
REPO = pathlib.Path(os.environ["REPO"]).resolve()
sys.path.insert(0, str(REPO / "controller")); sys.path.insert(0, str(REPO))
from app.operator import decision
from app.operator.models import HealthEvent
from termini.decision.types import DecisionResponse

VM = "01M4349RPXWDC52ZDAVMPCMJSH"; NODE = "01M4349RPTT29RNTC6VBCXQ0RC"
ev = HealthEvent(event_id="01M4349RQ09HMAX6RC9YPYQH4W", node_id=NODE,
                 rule_id="oom-guard-01", type="oom", metric="ram_used_bytes",
                 value=95.0, threshold=">= 95%", fired_at="2026-10-16T08:00:00.000Z",
                 breach_started_at="2026-10-16T07:59:58.000Z",
                 evidence=[{"seq": 1, "ram_used_bytes": 32641751449}])
ctx = decision.DecisionContext(
    event=ev, state={"seq": 1, "ram_used_bytes": 32_000_000_000, "ram_total_bytes": 34_000_000_000},
    evidence=[{"seq": 1, "lxd_instances": [{"vm_id": VM, "state": "running", "ram_used_bytes": 10_000_000_000}]}],
    instances=[{"vm_id": VM, "state": "running", "ram_used_bytes": 10_000_000_000}],
    instance_names={VM: "agent-c1"}, default_instance_name="agent",
    allowed_actions=list(decision.ACTION_IDS), project_id="P")
det = decision.DeterministicDecisionProvider(playbooks={}, instance_names={VM: "agent-c1"})
prov = decision.ModelDecisionProvider(
    deterministic=det,
    decide_fn=lambda i: DecisionResponse(action_id="instance.restart", confidence=0.1,
        rationale="restart", params={"instance_name": "heaviest"}, raw="{}"),
    instance_names={VM: "agent-c1"}, default_instance_name="agent")
res = prov.decide(ev, ctx)
print("source:", res.source, "| fallback_reason:", res.fallback_reason)
act = next(s for s in res.trace.steps if s["step"] == "action_decided")
print("action_decided detail:", act["detail"])
print("action_decided state:", act["state"])
assert res.source == "fallback"
assert "reason=low_confidence" in act["detail"]
assert "rejecting_gate=G6" in act["detail"]
print("OK: fallback trace carries reason + rejecting gate")
