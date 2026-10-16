#!/usr/bin/env python3
"""Independent probe: the model trace's model_call step carries endpoint class +
answered_by from the egress record (forwarded case), not just in the live stub.
"""
import json, os, pathlib, sys
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
FWD = {"backend": "gemini-proxy", "model": "deepseek-flash",
       "answered_by": "forwarded-managed-model", "destination": "aiplatform.googleapis.com",
       "destination_class": "external"}
prov = decision.ModelDecisionProvider(
    deterministic=det,
    decide_fn=lambda i: DecisionResponse(action_id="instance.restart", confidence=0.9,
        rationale="restart heaviest", params={"instance_name": "heaviest"},
        raw=json.dumps({"action_id": "instance.restart"})),
    instance_names={VM: "agent-c1"}, default_instance_name="agent",
    egress_probe=lambda: dict(FWD))
res = prov.decide(ev, ctx)
mc = next(s for s in res.trace.steps if s["step"] == "model_call")
print("model_call detail:", mc["detail"])
print("decided_by:", res.trace.decided_by, "| source:", res.source)
assert "endpoint=external" in mc["detail"]
assert "answered_by=forwarded-managed-model" in mc["detail"]
print("OK: endpoint class + answered_by carried on the model trace")
