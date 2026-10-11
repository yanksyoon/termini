"""Unit tests for the controller-side hard guardrail (G1–G8).

Each guardrail rule is mapped to the response field it constrains
(operator-decision.md §7): G1 (JSON parse), G2 (schema), G3 (action whitelist),
G4 (param key whitelist — strip), G5 (param bounds / instance resolution —
clamp or fallback), G6 (confidence gate), G7 (rationale sanitization), G8
(idempotency — deferred). Clamp cases assert G4/G5 clamp rather than reject.

The guardrail is exercised directly with ``DecisionResponse`` / raw dict / raw
string inputs — no model, no network. It is the enforcement point: a
well-shaped response that violates the whitelist, the bounds, or the confidence
gate must be rejected/clamped/gated here (the adapter deliberately does not,
per tests/test_decision_adapter.py TestAdapterBoundary).
"""

from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO))

from app.operator.decision import (  # noqa: E402
    DecisionContext,
    Guardrail,
)
from app.operator.models import HealthEvent, RemediationAction  # noqa: E402
from termini.decision.types import DecisionResponse  # noqa: E402

VM_ID = "01M4349RPXWDC52ZDAVMPCMJSH"
NODE_ID = "01M4349RPTT29RNTC6VBCXQ0RC"


def make_event() -> HealthEvent:
    return HealthEvent(
        event_id="01M4349RQ09HMAX6RC9YPYQH4W",
        node_id=NODE_ID,
        rule_id="oom-guard-01",
        type="oom",
        metric="ram_used_bytes",
        value=95.0,
        threshold=">= 95% of ram_total_bytes",
        fired_at="2026-10-04T08:00:00.000Z",
    )


def make_context(*, instance_names=None, instances=None, evidence=None) -> DecisionContext:
    insts = instances if instances is not None else [
        {"vm_id": VM_ID, "state": "running", "ram_used_bytes": 10_000_000_000},
    ]
    ev = evidence if evidence is not None else [
        {"seq": 1, "lxd_instances": insts},
    ]
    return DecisionContext(
        event=make_event(),
        state={"seq": 2, "ram_used_bytes": 32_000_000_000},
        evidence=ev,
        instances=insts,
        instance_names=instance_names or {VM_ID: "agent-c1"},
        default_instance_name="agent",
        allowed_actions=["instance.restart", "instance.stop",
                         "instance.delete", "image.prune"],
        project_id="01M4349RPSZFTB60AVJEG0ZZY9",
    )


def make_response(action_id="instance.restart", params=None, confidence=0.9,
                  rationale="restart the heaviest instance", schema_version=1,
                  request_id=None) -> DecisionResponse:
    return DecisionResponse(
        action_id=action_id,
        confidence=confidence,
        rationale=rationale,
        params=params if params is not None else {"instance_name": "heaviest"},
        schema_version=schema_version,
        request_id=request_id,
    )


class GuardrailRejectionTest(unittest.TestCase):
    def setUp(self):
        self.guardrail = Guardrail()
        self.ctx = make_context()

    # G1 — JSON parse
    def test_g1_non_json_string_is_parse_error(self):
        result = self.guardrail.validate("not valid json {", self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.reject_reason, "parse_error")

    def test_g1_non_object_json_is_schema_violation(self):
        result = self.guardrail.validate("[1, 2, 3]", self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.reject_reason, "schema_violation")

    # G2 — schema validation
    def test_g2_wrong_schema_version(self):
        result = self.guardrail.validate(make_response(schema_version=2), self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.reject_reason, "schema_violation")

    def test_g2_missing_action_id(self):
        result = self.guardrail.validate(
            {"schema_version": 1, "confidence": 0.9, "rationale": "x"}, self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.reject_reason, "schema_violation")

    def test_g2_non_numeric_confidence(self):
        result = self.guardrail.validate(
            {"schema_version": 1, "action_id": "instance.restart",
             "confidence": "high", "rationale": "x"}, self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.reject_reason, "schema_violation")

    # G3 — action whitelist
    def test_g3_unknown_action_is_action_not_allowed(self):
        result = self.guardrail.validate(make_response(action_id="rm -rf /"), self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.reject_reason, "action_not_allowed")

    def test_g3_known_action_not_in_allowed_set_is_rejected(self):
        guardrail = Guardrail(allowed_actions=["instance.stop", "image.prune"])
        result = guardrail.validate(make_response(action_id="instance.restart"), self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.reject_reason, "action_not_allowed")

    # G5 — instance resolution -> invalid_target
    def test_g5_unresolvable_instance_is_invalid_target(self):
        result = self.guardrail.validate(
            make_response(params={"instance_name": "nonexistent-vm"}), self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.reject_reason, "invalid_target")

    def test_g5_missing_required_instance_name_is_invalid_target(self):
        result = self.guardrail.validate(make_response(params={}), self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.reject_reason, "invalid_target")

    # G6 — confidence gate
    def test_g6_low_confidence_is_low_confidence(self):
        result = self.guardrail.validate(make_response(confidence=0.1), self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.reject_reason, "low_confidence")

    # G7 — rationale C2 scan
    def test_g7_c2_signature_in_rationale_is_schema_violation(self):
        result = self.guardrail.validate(
            make_response(rationale="contact admin@example.com to proceed"), self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.reject_reason, "schema_violation")


class GuardrailClampTest(unittest.TestCase):
    """G4/G5 clamp (never reject on numeric/unknown-key issues)."""

    def setUp(self):
        self.guardrail = Guardrail()
        self.ctx = make_context()

    def test_g4_unknown_param_key_is_stripped_not_passed_through(self):
        result = self.guardrail.validate(
            make_response(params={"instance_name": "heaviest", "extra": "boom"}), self.ctx)
        self.assertTrue(result.ok)
        self.assertNotIn("extra", result.action.params)
        self.assertTrue(any(d["rule"] == "G4" for d in result.decisions))

    def test_g5_timeout_s_clamped_to_max(self):
        result = self.guardrail.validate(
            make_response(params={"instance_name": "heaviest", "timeout_s": 999}), self.ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.action.params["timeout_s"], 120)

    def test_g5_timeout_s_clamped_to_min(self):
        result = self.guardrail.validate(
            make_response(params={"instance_name": "heaviest", "timeout_s": 0}), self.ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.action.params["timeout_s"], 1)

    def test_g5_force_non_bool_coerced_to_default(self):
        result = self.guardrail.validate(
            make_response(params={"instance_name": "heaviest", "force": "yes"}), self.ctx)
        self.assertTrue(result.ok)
        self.assertIs(result.action.params["force"], True)

    def test_g7_long_rationale_truncated(self):
        # A long but C0-safe rationale (spaces break base64/blob signatures) is
        # truncated to 1024 chars rather than rejected.
        result = self.guardrail.validate(
            make_response(rationale="restart the instance to reclaim memory " * 60),
            self.ctx)
        self.assertTrue(result.ok)
        self.assertLessEqual(len(result.action.reason), 1024)


class GuardrailHappyPathTest(unittest.TestCase):
    def setUp(self):
        self.guardrail = Guardrail()
        self.ctx = make_context()

    def test_heaviest_resolves_to_heaviest_vm_name(self):
        # Two instances: the second holds more RAM, so "heaviest" resolves to it.
        ctx = make_context(
            instance_names={VM_ID: "agent-a", "01M4444RPXWDC52ZDAVMPCMJSH": "agent-b"},
            instances=[
                {"vm_id": VM_ID, "state": "running", "ram_used_bytes": 1_000_000_000},
                {"vm_id": "01M4444RPXWDC52ZDAVMPCMJSH", "state": "running",
                 "ram_used_bytes": 50_000_000_000},
            ],
            evidence=[{"seq": 1, "lxd_instances": [
                {"vm_id": VM_ID, "ram_used_bytes": 1_000_000_000},
                {"vm_id": "01M4444RPXWDC52ZDAVMPCMJSH", "ram_used_bytes": 50_000_000_000},
            ]}],
        )
        result = self.guardrail.validate(make_response(params={"instance_name": "heaviest"}), ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.action.params["instance_name"], "agent-b")

    def test_vm_id_target_resolves_to_name(self):
        result = self.guardrail.validate(
            make_response(params={"instance_name": VM_ID}), self.ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.action.params["instance_name"], "agent-c1")

    def test_valid_response_builds_action_and_records_g8(self):
        result = self.guardrail.validate(make_response(), self.ctx)
        self.assertTrue(result.ok)
        self.assertIsInstance(result.action, RemediationAction)
        self.assertEqual(result.action.kind, "instance.restart")
        self.assertEqual(result.action.params["instance_name"], "agent-c1")
        self.assertEqual(result.confidence, 0.9)
        self.assertEqual(result.action.rule_id, "oom-guard-01")
        self.assertTrue(any(d["rule"] == "G8" for d in result.decisions))

    def test_image_prune_needs_no_instance_name(self):
        result = self.guardrail.validate(
            make_response(action_id="image.prune", params={}), self.ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.action.kind, "image.prune")
        self.assertEqual(result.action.params, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
