"""Prompt-side + falsification verification for the metric-expansion diagnosis.

1. Parse the assembled request (stub-model-requests.jsonl) from MY OWN fresh run
   and prove every substituted field is C0-registered.
2. Falsify the diagnosis use: old-shaped sample (no new fields) degrades honestly;
   enriched sample changes the output observably; a planted non-C0 field fires the
   §7.2 assertion and the model is never called.
"""
import json
import sys

WT = "/home/ubuntu/.hermes/profiles/verifier/cache/scratch/metrics-verify-1792189188"
LIVE = "/home/ubuntu/.hermes/profiles/verifier/cache/scratch/verify-diagnosis-1792189616/live"
sys.path.insert(0, WT + "/controller")
sys.path.insert(0, WT + "/tests")
sys.path.insert(0, WT)

from app import fields as F  # noqa: E402
from app.operator import decision  # noqa: E402
from app.operator.external_call import (  # noqa: E402
    assert_allowed_classes, external_model_call, ExternalCallViolation,
)
from app.operator.models import HealthEvent  # noqa: E402
from termini.decision.types import DecisionResponse, DecisionError  # noqa: E402

VM_ID = "01M4349RPXWDC52ZDAVMPCMJSH"
NODE_ID = "01M4349RPTT29RNTC6VBCXQ0RC"
PROJECT_ID = "01M4349RPSZFTB60AVJEG0ZZY9"

# ---------------------------------------------------------------------------
print("=== A. The assembled request contains ONLY C0-registered fields ===")
reqs = [json.loads(l) for l in open(LIVE + "/stub-model-requests.jsonl")]
last = reqs[-1]
msgs = last["body"]["messages"]
user_content = msgs[-1]["content"]
# The user message embeds the DecisionInput as compact JSON between markers.
marker = "input:\n\n"
decision_json = user_content.split(marker, 1)[1].split("\n\nReturn", 1)[0]
payload = json.loads(decision_json)
print("assembled DecisionInput top-level keys:", sorted(payload.keys()))
print("derived object (from the assembled request):", json.dumps(payload.get("derived")))

# Every substituted field must be C0 / transformed-C1. Collect all field names.
def leaf_names(obj, prefix=""):
    names = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            names.append(prefix + k)
            names += leaf_names(v, prefix + k + ".")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            names += leaf_names(v, prefix + f"[{i}].")
    return names

state_fields = sorted(set(leaf_names(payload.get("state") or {})))
evidence_fields = sorted(set(leaf_names(payload.get("evidence") or [])))
derived_fields = sorted(set(leaf_names(payload.get("derived") or {})))

# The allowlist is fields.py: SAMPLE_FIELDS keys + container keys + their subfields.
allowed_top = set(F.SAMPLE_FIELDS) | set(F.SAMPLE_CONTAINERS)
import re
_IDX = re.compile(r"^\[\d+\]$")

def is_allowed(field_path):
    # Drop array-index tokens ("[0]") — they are position, not field names.
    parts = [p for p in field_path.split(".") if not _IDX.match(p)]
    top = parts[0]
    if top not in allowed_top:
        return False
    if top in F.SAMPLE_FIELDS:
        return len(parts) == 1
    container = F.SAMPLE_CONTAINERS[top]
    sub = set(container.get("fields") or {})
    return len(parts) == 1 or (len(parts) == 2 and parts[1] in sub)

bad_state = [f for f in state_fields if not is_allowed(f)]
bad_evidence = [f for f in evidence_fields if not is_allowed(f)]
print("state field count:", len(state_fields), "non-C0:", bad_state)
print("evidence field count:", len(evidence_fields), "non-C0:", bad_evidence)
print("derived keys:", derived_fields)
assert not bad_state and not bad_evidence, "non-C0 field reached the model"
# The derived keys must be exactly the frozen set.
assert set(payload["derived"].keys()) <= {"window_samples", "oom_kill_count_delta",
        "pgmajfault_count_delta", "tcp_retransmit_segs_delta"}

# The §7.2 assertion on the assembled payload: must be clean.
viol = assert_allowed_classes(payload)
print("assert_allowed_classes(assembled request):", viol)
assert viol == []
print("PASS: every substituted field is C0-registered; assertion clean\n")

# ---------------------------------------------------------------------------
print("=== B. FALSIFY: old-shaped sample (no new fields) degrades honestly ===")

def old_sample(seq):
    return {"seq": seq, "ts": f"2026-10-04T08:00:{seq:02d}Z",
            "ram_total_bytes": 34_359_738_368, "ram_used_bytes": 34_000_000_000,
            "cpu_util_pct": 88.0}  # NO pressure, NO oom/pgmajfault/retransmit/conntrack

def new_sample(seq, oom=None, pgm=None, retr=None):
    s = dict(old_sample(seq))
    s.update({"mem_available_bytes": 2_000_000_000, "conntrack_count": 457,
              "conntrack_max": 262_144,
              "pressure": {"cpu_some_avg10": 12.3, "io_some_avg10": 0.0,
                           "memory_some_avg10": 91.7, "memory_full_avg10": 0.0},
              "disk": [{"path": "/dev/sda", "total_bytes": 274_877_906_944,
                        "free_bytes": 200_000_000_000, "write_latency_ms": 3.0,
                        "mount_ro": False, "io_ticks_ms": 62_361_848,
                        "io_in_flight": 0, "inode_used": 1_129_359,
                        "inode_total": 12_976_128}],
              "net": [{"iface": "eth0", "rx_bytes": 1, "tx_bytes": 1,
                       "latency_ms": 0.8, "rx_errors": 0, "tx_errors": 0,
                       "rx_dropped": 0, "tx_dropped": 0}],
              "lxd_instances": [{"vm_id": VM_ID, "state": "running",
                                 "ram_used_bytes": 10_000_000_000,
                                 "oom_kill_count": 0, "pgmajfault_count": 30_730}]})
    if oom is not None:
        s["oom_kill_count"] = oom
    if pgm is not None:
        s["pgmajfault_count"] = pgm
    if retr is not None:
        s["tcp_retransmit_segs"] = retr
    return s

def make_event():
    return HealthEvent(event_id="01M4349RQ09HMAX6RC9YPYQH4W", node_id=NODE_ID,
        rule_id="oom-guard-01", type="oom", metric="ram_used_bytes", value=95.0,
        threshold=">= 95% of ram_total_bytes for 2 consecutive samples",
        fired_at="2026-10-04T08:00:00.000Z", breach_started_at="2026-10-04T07:59:58.000Z",
        evidence=[{"seq": 1050, "ram_used_bytes": 32641751449}])

def make_ctx(state, evidence):
    return decision.DecisionContext(
        event=make_event(), state=state, evidence=evidence,
        instances=[{"vm_id": VM_ID, "state": "running", "ram_used_bytes": 10_000_000_000,
                    "oom_kill_count": 0, "pgmajfault_count": 30_730}],
        instance_names={VM_ID: "agent-c1"}, default_instance_name="agent",
        allowed_actions=list(decision.ACTION_IDS), project_id=PROJECT_ID)

# B1: old-shaped sample -> derived has NO deltas (never fabricated), model still
#     produces a labelled incident record.
old_ctx = make_ctx(old_sample(2), [old_sample(1)])
old_input = decision.build_decision_input(old_ctx).to_dict()
print("old-shaped derived:", json.dumps(old_input["derived"]))
assert old_input["derived"] == {"window_samples": 1}  # one state sample, no delta keys
assert "pressure" not in old_input["state"]
print("  -> old-shaped: derived={window_samples} only (no fabricated delta), pressure absent")

# B2: model UNavailable with old-shaped sample -> fallback labelled, record produced.
def det():
    return decision.DeterministicDecisionProvider(
        playbooks={}, instance_names={VM_ID: "agent-c1"}, default_instance_name="agent")

prov = decision.ModelDecisionProvider(
    deterministic=det(),
    decide_fn=lambda inp: DecisionError("inference_unavailable", "down"),
    instance_names={VM_ID: "agent-c1"}, default_instance_name="agent")
res = prov.decide(make_event(), old_ctx)
print("  fallback: source=%s fallback_reason=%s incident.generated=%s" %
      (res.source, res.fallback_reason, res.incident["generated"]))
print("  fallback incident evidence:", res.incident["evidence"])
assert res.source == "fallback" and res.fallback_reason == "inference_unavailable"
assert res.incident["generated"] == "fallback"
assert res.incident["evidence"][0]["field"] == "ram_used_bytes"  # event C0 only

# B3: enriched sample -> derived deltas present, trace names the new fields.
enr_ctx = make_ctx(new_sample(2, oom=13, pgm=145, retr=1018),
                   [new_sample(1, oom=10, pgm=100, retr=1000),
                    new_sample(2, oom=13, pgm=145, retr=1018)])
enr_input = decision.build_decision_input(enr_ctx).to_dict()
print("enriched derived:", json.dumps(enr_input["derived"]))
assert enr_input["derived"]["oom_kill_count_delta"] == 3
assert enr_input["derived"]["pgmajfault_count_delta"] == 45
assert enr_input["derived"]["tcp_retransmit_segs_delta"] == 18

# B4: enriched path with a stub model that quotes pressure first -> incident
#     evidence leads with pressure, decided_by=model.
def stub_ok(inp):
    st = inp.state if hasattr(inp, "state") else inp["state"]
    return DecisionResponse(action_id="instance.restart", confidence=0.9,
        rationale="memory-bound stall; restart the heaviest instance",
        params={"instance_name": "heaviest", "force": True, "timeout_s": 30},
        incident_title="Memory exhaustion", explanation="pressure high",
        evidence=[{"field": "pressure.memory_some_avg10", "value": st["pressure"]["memory_some_avg10"], "seq": st["seq"]},
                  {"field": "ram_used_bytes", "value": st["ram_used_bytes"], "seq": st["seq"]}],
        actions=[{"action_kind": "instance.restart", "target": "heaviest"}])
prov2 = decision.ModelDecisionProvider(
    deterministic=det(), decide_fn=stub_ok,
    instance_names={VM_ID: "agent-c1"}, default_instance_name="agent")
res2 = prov2.decide(make_event(), enr_ctx)
print("enriched: source=%s decided_by=%s fallback_reason=%s" %
      (res2.source, res2.trace.decided_by, res2.fallback_reason))
print("enriched incident evidence fields:", [e["field"] for e in res2.incident["evidence"]])
assert res2.source == "model" and res2.trace.decided_by == "model"
assert res2.incident["evidence"][0]["field"] == "pressure.memory_some_avg10"
trace_fields = [s for s in res2.trace.steps if s["step"] == "evidence_window"][0]["detail"]
print("enriched trace evidence_window:", trace_fields[:160], "...")
assert "pressure" in trace_fields

# B5: a planted non-C0 field fires the §7.2 assertion; the model is NEVER called.
bad_ctx = make_ctx(new_sample(2), [new_sample(1)])
bad_ctx.state = dict(bad_ctx.state, cmdline="/bin/sh -c steal")  # C2 field name
sent = []
def transport(inp):
    sent.append(inp)
    return DecisionResponse(action_id="instance.restart", confidence=0.9,
                            rationale="x", params={"instance_name": "heaviest"})
prov3 = decision.ModelDecisionProvider(
    deterministic=det(), decide_fn=transport,
    instance_names={VM_ID: "agent-c1"}, default_instance_name="agent")
res3 = prov3.decide(make_event(), bad_ctx)
print("planted cmdline -> source=%s fallback_reason=%s model_called=%s" %
      (res3.source, res3.fallback_reason, bool(sent)))
assert res3.source == "fallback" and res3.fallback_reason == "schema_violation"
assert sent == []
# Quote the assertion output directly.
payload_bad = decision.build_decision_input(bad_ctx).to_dict()
print("assert_allowed_classes(planted):", assert_allowed_classes(payload_bad))

print("\nALL PROMPT-SIDE + FALSIFICATION CHECKS PASSED")
