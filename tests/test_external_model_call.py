"""Tests for ``externalModelCall()`` — the governance §7.2/§9.3 boundary hook.

The hook is normative in three places (``governance.md`` §7.2 and §9.3,
``controller-api.md`` §117) and was previously documented but unimplemented:
a whole-tree search found every hit in ``.md`` files and none under
``controller/app/``. These tests pin the behaviour that makes the claim
enforceable — that a payload whose substituted fields are not provably C0 /
transformed-C1 is **never sent**, and that the guard is wired into the
model decision path (so it applies to the on-prem node-c endpoint too, not just
an "external" one).
"""

from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO))

import termini.decision as termini_decision  # noqa: E402
from app.operator.decision import (  # noqa: E402
    DecisionContext,
    DeterministicDecisionProvider,
    ModelDecisionProvider,
    build_decision_input,
)
from app.operator.external_call import (  # noqa: E402
    ExternalCallViolation,
    assert_allowed_classes,
    external_model_call,
    template_version,
)
from app.operator.models import HealthEvent  # noqa: E402
from termini.decision.types import DecisionResponse  # noqa: E402

VM_ID = "01M4349RPXWDC52ZDAVMPCMJSH"
NODE_ID = "01M4349RPTT29RNTC6VBCXQ0RC"
PROJECT_ID = "01M4349RPSZFTB60AVJEG0ZZY9"


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


def clean_sample(seq: int = 1) -> dict:
    """A telemetry sample exactly as the controller stores + re-emits it: C0
    metrics plus C1 identifiers already transformed."""
    return {
        "seq": seq,
        "ts": "2026-10-04T08:00:00.000Z",
        "uptime_sec": 3600,
        "cpu_util_pct": 31.2,
        "ram_total_bytes": 68_719_476_736,
        "ram_used_bytes": 66_000_000_000,
        "os_name": "Ubuntu",
        "os_version": "24.04",
        "kernel_version": "6.8.0-142-generic",
        "lxd_cluster_role": "member",
        "hostname": "0123456789abcdef",
        "ip_local": "10.14.100.0/24",
        "mac_address": "fedcba9876543210",
        "project_id": PROJECT_ID,
        "disk": [{"path": "/dev/sda", "write_latency_ms": 3.0, "mount_ro": False}],
        "lxd_instances": [{"vm_id": VM_ID, "state": "running", "ram_used_bytes": 10}],
    }


def make_context(**overrides) -> DecisionContext:
    insts = [{"vm_id": VM_ID, "state": "running", "ram_used_bytes": 10_000_000_000}]
    ctx = DecisionContext(
        event=make_event(),
        state=clean_sample(2),
        evidence=[clean_sample(1)],
        instances=insts,
        instance_names={VM_ID: "agent-c1"},
        default_instance_name="agent",
        allowed_actions=["instance.restart", "instance.stop",
                         "instance.delete", "image.prune"],
        project_id=PROJECT_ID,
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def payload_of(ctx: DecisionContext) -> dict:
    return build_decision_input(ctx).to_dict()


class ClassAssertionTest(unittest.TestCase):
    """§7.2 — every substituted field must be C0 / transformed C1."""

    def test_clean_payload_has_no_violations(self):
        self.assertEqual(assert_allowed_classes(payload_of(make_context())), [])

    def test_clean_evidence_window_has_no_violations(self):
        ctx = make_context(evidence=[clean_sample(i) for i in range(1, 21)])
        self.assertEqual(assert_allowed_classes(payload_of(ctx)), [])

    def test_c2_field_name_in_state_is_rejected(self):
        ctx = make_context()
        ctx.state = dict(ctx.state, app_logs=["user clicked buy"])
        viol = assert_allowed_classes(payload_of(ctx))
        self.assertTrue(any("app_logs" in v for v in viol), viol)

    def test_c2_field_name_in_evidence_is_rejected(self):
        ctx = make_context()
        bad = dict(clean_sample(1), request_body="POST /charge ...")
        ctx.evidence = [bad]
        viol = assert_allowed_classes(payload_of(ctx))
        self.assertTrue(any("request_body" in v for v in viol), viol)

    def test_unknown_field_is_not_provably_c0(self):
        ctx = make_context()
        ctx.state = dict(ctx.state, mystery_blob="opaque")
        viol = assert_allowed_classes(payload_of(ctx))
        self.assertTrue(any("mystery_blob" in v for v in viol), viol)

    def test_unknown_container_subfield_is_rejected(self):
        ctx = make_context()
        ctx.state = dict(ctx.state, disk=[{"path": "/dev/sda", "note": "free text"}])
        viol = assert_allowed_classes(payload_of(ctx))
        self.assertTrue(any("note" in v for v in viol), viol)

    def test_c2_signature_in_an_allowed_field_is_rejected(self):
        ctx = make_context()
        ctx.state = dict(ctx.state, kernel_version="mail jane@acme.example now")
        viol = assert_allowed_classes(payload_of(ctx))
        self.assertTrue(any("email" in v for v in viol), viol)

    def test_card_signature_in_an_allowed_field_is_rejected(self):
        ctx = make_context()
        ctx.state = dict(ctx.state, kernel_version="4111111111111111")
        viol = assert_allowed_classes(payload_of(ctx))
        self.assertTrue(viol)

    def test_untransformed_c1_identifier_is_rejected(self):
        ctx = make_context()
        ctx.state = dict(ctx.state, hostname="prod-db-01")
        viol = assert_allowed_classes(payload_of(ctx))
        self.assertTrue(any("hostname" in v for v in viol), viol)

    def test_unknown_top_level_field_is_rejected(self):
        payload = payload_of(make_context())
        payload["notes"] = "free text"
        viol = assert_allowed_classes(payload)
        self.assertTrue(any("notes" in v for v in viol), viol)

    def test_target_resource_unknown_key_is_rejected(self):
        payload = payload_of(make_context())
        payload["target_resource"]["secret_hint"] = "x"
        viol = assert_allowed_classes(payload)
        self.assertTrue(any("secret_hint" in v for v in viol), viol)

    def test_instance_key_outside_c0_schema_is_rejected(self):
        payload = payload_of(make_context())
        payload["target_resource"]["instances"][0]["process_list"] = "ps aux"
        viol = assert_allowed_classes(payload)
        self.assertTrue(any("process_list" in v for v in viol), viol)


class WrapperTest(unittest.TestCase):
    """§9.3 — assert, audit before, call, audit after."""

    def test_send_is_called_and_result_returned_on_clean_payload(self):
        ctx = make_context()
        calls = []

        def send():
            calls.append(1)
            return "RESULT"

        self.assertEqual(external_model_call(build_decision_input(ctx), send), "RESULT")
        self.assertEqual(len(calls), 1)

    def test_send_is_never_called_on_violation(self):
        ctx = make_context()
        ctx.state = dict(ctx.state, app_logs=["secret log line"])
        calls = []
        with self.assertRaises(ExternalCallViolation):
            external_model_call(build_decision_input(ctx), lambda: calls.append(1))
        self.assertEqual(calls, [], "a non-C0 payload must not be sent")

    def test_template_version_mismatch_is_a_violation(self):
        ctx = make_context()
        with self.assertRaises(ExternalCallViolation) as cm:
            external_model_call(
                build_decision_input(ctx), lambda: "x",
                template_version_expected="operator-decision v1",
                template_version_actual="operator-decision v2",
            )
        self.assertTrue(any("template version" in v for v in cm.exception.violations))

    def test_expected_template_version_is_the_frozen_one(self):
        self.assertEqual(template_version(), "operator-decision v1")

    def test_audit_sink_records_before_and_after(self):
        ctx = make_context()
        seen = []
        external_model_call(
            build_decision_input(ctx), lambda: "OK",
            destination="http://10.10.0.13:8080", audit_sink=seen.append,
        )
        self.assertEqual([r["phase"] for r in seen], ["before", "after"])
        self.assertEqual(seen[0]["outcome"], "ok")
        self.assertEqual(seen[1]["outcome"], "returned")
        self.assertEqual(seen[0]["destination"], "http://10.10.0.13:8080")
        self.assertEqual(seen[0]["template_version"], "operator-decision v1")
        self.assertTrue(seen[0]["payload_sha256"])

    def test_audit_sink_records_a_rejection(self):
        ctx = make_context()
        ctx.state = dict(ctx.state, app_logs=["x"])
        seen = []
        with self.assertRaises(ExternalCallViolation):
            external_model_call(build_decision_input(ctx), lambda: "x",
                                audit_sink=seen.append)
        self.assertEqual(seen[0]["phase"], "before")
        self.assertEqual(seen[0]["outcome"], "rejected")
        self.assertTrue(seen[0]["violations"])

    def test_sink_failure_does_not_break_the_call(self):
        ctx = make_context()

        def boom(_record):
            raise RuntimeError("sink down")

        self.assertEqual(
            external_model_call(build_decision_input(ctx), lambda: "OK",
                                audit_sink=boom),
            "OK",
        )


class WiringTest(unittest.TestCase):
    """The guard must be on the real decision path, not just available."""

    def setUp(self):
        self._orig = termini_decision.decide
        self.sent = []

    def tearDown(self):
        termini_decision.decide = self._orig

    def _install_fake_transport(self, response):
        def fake(decision_input, config=None):
            self.sent.append(decision_input)
            return response
        termini_decision.decide = fake

    def test_clean_context_reaches_the_transport_as_source_model(self):
        self._install_fake_transport(DecisionResponse(
            action_id="instance.restart",
            params={"instance_name": "heaviest"},
            confidence=0.9, rationale="ram pressure", schema_version=1,
        ))
        provider = ModelDecisionProvider(deterministic=DeterministicDecisionProvider(
            playbooks={}, instance_names={VM_ID: "agent-c1"}))
        result = provider.decide(make_event(), make_context())
        self.assertEqual(result.source, "model")
        self.assertEqual(len(self.sent), 1)

    def test_c2_bearing_context_falls_back_and_never_calls_the_transport(self):
        self._install_fake_transport(DecisionResponse(
            action_id="instance.restart", params={}, confidence=0.9,
            rationale="x", schema_version=1,
        ))
        ctx = make_context()
        ctx.state = dict(ctx.state, app_logs=["customer payload line"])
        provider = ModelDecisionProvider(deterministic=DeterministicDecisionProvider(
            playbooks={}, instance_names={VM_ID: "agent-c1"}))
        result = provider.decide(make_event(), ctx)
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.fallback_reason, "schema_violation")
        self.assertEqual(self.sent, [], "the model must not be called")

    def test_untransformed_c1_context_falls_back(self):
        self._install_fake_transport(DecisionResponse(
            action_id="instance.restart", params={}, confidence=0.9,
            rationale="x", schema_version=1,
        ))
        ctx = make_context()
        ctx.state = dict(ctx.state, hostname="prod-db-01")
        provider = ModelDecisionProvider(deterministic=DeterministicDecisionProvider(
            playbooks={}, instance_names={VM_ID: "agent-c1"}))
        result = provider.decide(make_event(), ctx)
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.fallback_reason, "schema_violation")
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
