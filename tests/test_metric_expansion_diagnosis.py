"""Tests for the metric-expansion half of the AI diagnosis (t_922b3268).

Covers the new C0 fields reaching the model, the derived-delta computation, the
C0/template assertion firing on a wrongly-classed field, and the parity
guarantees (fallback still records, deterministic mode stays byte-identical).

The model is always a stub — no network, no model call.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO))

from app.operator import decision  # noqa: E402
from app.operator.external_call import (  # noqa: E402
    ExternalCallViolation,
    assert_allowed_classes,
    external_model_call,
)
from app.operator.models import HealthEvent, RemediationAction  # noqa: E402
from termini.decision.types import (  # noqa: E402
    DecisionError,
    DecisionResponse,
)

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
        threshold=">= 95% of ram_total_bytes for 2 consecutive samples",
        fired_at="2026-10-04T08:00:00.000Z",
        breach_started_at="2026-10-04T07:59:58.000Z",
        evidence=[{"seq": 1050, "ram_used_bytes": 32641751449}],
    )


def sample(seq: int, *, oom_kill: int | None = None, pgmajfault: int | None = None,
           retransmit: int | None = None) -> dict:
    """A C0 sample carrying the §4.12–§4.14 fields, with the cumulative counters
    settable per sample (``None`` omits the key, matching omit-not-fabricate)."""
    s: dict = {
        "seq": seq,
        "ts": f"2026-10-04T08:00:{seq:02d}Z",
        "ram_total_bytes": 34_359_738_368,
        "ram_used_bytes": 34_000_000_000,
        "cpu_util_pct": 88.0,
        "mem_available_bytes": 2_000_000_000,
        "conntrack_count": 457,
        "conntrack_max": 262_144,
        "pressure": {
            "cpu_some_avg10": 12.3,
            "io_some_avg10": 0.0,
            "memory_some_avg10": 91.7,
            "memory_full_avg10": 0.0,
        },
        "disk": [{"path": "/dev/sda", "total_bytes": 274_877_906_944,
                  "free_bytes": 200_000_000_000, "write_latency_ms": 3.0,
                  "mount_ro": False, "io_ticks_ms": 62_361_848,
                  "io_in_flight": 0, "inode_used": 1_129_359,
                  "inode_total": 12_976_128}],
        "net": [{"iface": "eth0", "rx_bytes": 1, "tx_bytes": 1, "latency_ms": 0.8,
                 "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0}],
        "lxd_instances": [{"vm_id": VM_ID, "state": "running",
                           "ram_used_bytes": 10_000_000_000,
                           "oom_kill_count": 0, "pgmajfault_count": 30_730}],
    }
    if oom_kill is not None:
        s["oom_kill_count"] = oom_kill
    if pgmajfault is not None:
        s["pgmajfault_count"] = pgmajfault
    if retransmit is not None:
        s["tcp_retransmit_segs"] = retransmit
    return s


def make_context(**overrides) -> decision.DecisionContext:
    insts = [{"vm_id": VM_ID, "state": "running", "ram_used_bytes": 10_000_000_000,
              "oom_kill_count": 0, "pgmajfault_count": 30_730}]
    ctx = decision.DecisionContext(
        event=make_event(),
        state=sample(2, oom_kill=40, pgmajfault=2_670_694, retransmit=297_295),
        evidence=[sample(1, oom_kill=37, pgmajfault=2_671_000, retransmit=297_277)],
        instances=insts,
        instance_names={VM_ID: "agent-c1"},
        default_instance_name="agent",
        allowed_actions=list(decision.ACTION_IDS),
        project_id=PROJECT_ID,
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


class DerivedDeltaTest(unittest.TestCase):
    """The controller-computed `derived` deltas over the evidence window (§3.1)."""

    def test_deltas_computed_over_the_window(self):
        evidence = [
            sample(1, oom_kill=10, pgmajfault=100, retransmit=1000),
            sample(2, oom_kill=13, pgmajfault=145, retransmit=1018),
        ]
        self.assertEqual(decision.build_window_deltas(evidence), {
            "window_samples": 2,
            "oom_kill_count_delta": 3,
            "pgmajfault_count_delta": 45,
            "tcp_retransmit_segs_delta": 18,
        })

    def test_missing_field_omits_the_delta(self):
        # The newest sample omits oom_kill_count -> no delta, never a fabricated 0.
        evidence = [sample(1, oom_kill=10), sample(2)]
        self.assertEqual(decision.build_window_deltas(evidence), {"window_samples": 2})

    def test_counter_reset_omits_the_delta(self):
        # newest < oldest (a reboot/reset) -> omitted, never a negative delta.
        evidence = [sample(1, oom_kill=10), sample(2, oom_kill=5)]
        self.assertEqual(decision.build_window_deltas(evidence), {"window_samples": 2})

    def test_single_sample_has_no_delta(self):
        self.assertEqual(decision.build_window_deltas([sample(1, oom_kill=10)]),
                         {"window_samples": 1})

    def test_empty_evidence_has_no_delta(self):
        self.assertEqual(decision.build_window_deltas([]), {"window_samples": 0})


class EvidenceReachesProviderTest(unittest.TestCase):
    """The new C0 fields (and their derived deltas) reach the model payload, and
    the §7.2 class assertion admits them as provably C0."""

    def test_new_fields_reach_the_payload(self):
        payload = decision.build_decision_input(make_context()).to_dict()
        state = payload["state"]
        self.assertIn("pressure", state)
        self.assertEqual(state["pressure"]["memory_some_avg10"], 91.7)
        self.assertIn("oom_kill_count", state)
        self.assertIn("mem_available_bytes", state)
        self.assertIn("conntrack_count", state)
        self.assertEqual(state["disk"][0]["io_ticks_ms"], 62_361_848)
        self.assertEqual(state["net"][0]["rx_errors"], 0)
        self.assertIn("oom_kill_count", state["lxd_instances"][0])
        # The evidence window carries them too.
        self.assertIn("pressure", payload["evidence"][0])

    def test_new_fields_are_provably_c0(self):
        payload = decision.build_decision_input(make_context()).to_dict()
        self.assertEqual(assert_allowed_classes(payload), [])

    def test_derived_deltas_reach_the_payload(self):
        payload = decision.build_decision_input(make_context()).to_dict()
        self.assertEqual(payload["derived"], {
            "window_samples": 1,
        })  # a one-sample window has no delta yet

    def test_derived_deltas_pass_the_class_assertion(self):
        ctx = make_context(evidence=[
            sample(1, oom_kill=10, pgmajfault=100, retransmit=1000),
            sample(2, oom_kill=13, pgmajfault=145, retransmit=1018),
        ])
        payload = decision.build_decision_input(ctx).to_dict()
        self.assertEqual(payload["derived"]["oom_kill_count_delta"], 3)
        self.assertEqual(assert_allowed_classes(payload), [])


class WronglyClassedFieldTest(unittest.TestCase):
    """A new field that is NOT registered / provably C0 fires the §7.2 assertion
    (the template-version/C0 guard) and the model is never called."""

    def test_unregistered_new_field_is_not_provably_c0(self):
        ctx = make_context()
        ctx.state = dict(ctx.state, pressure_bogus=1.0)  # not on the allowlist
        payload = decision.build_decision_input(ctx).to_dict()
        viol = assert_allowed_classes(payload)
        self.assertTrue(any("pressure_bogus" in v for v in viol), viol)

    def test_wrongly_classed_new_field_falls_back_and_never_calls_model(self):
        ctx = make_context()
        ctx.state = dict(ctx.state, pressure_bogus=1.0)
        sent = []

        def transport(inp):
            sent.append(inp)
            return DecisionResponse(action_id="instance.restart", confidence=0.9,
                                    rationale="x", params={"instance_name": "heaviest"})

        provider = decision.ModelDecisionProvider(
            deterministic=decision.DeterministicDecisionProvider(
                playbooks={}, instance_names={VM_ID: "agent-c1"}),
            decide_fn=transport, instance_names={VM_ID: "agent-c1"},
            default_instance_name="agent")
        result = provider.decide(make_event(), ctx)
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.fallback_reason, "schema_violation")
        self.assertEqual(sent, [], "a non-C0 field must never reach the model")

    def test_derived_with_unknown_key_is_refused(self):
        payload = decision.build_decision_input(make_context()).to_dict()
        payload["derived"]["invented_delta"] = 1
        viol = assert_allowed_classes(payload)
        self.assertTrue(any("invented_delta" in v for v in viol), viol)

    def test_derived_with_wrong_type_is_refused(self):
        payload = decision.build_decision_input(make_context()).to_dict()
        payload["derived"]["oom_kill_count_delta"] = "three"
        viol = assert_allowed_classes(payload)
        self.assertTrue(any("oom_kill_count_delta" in v for v in viol), viol)

    def test_external_model_call_raises_on_wrongly_classed_field(self):
        ctx = make_context()
        ctx.state = dict(ctx.state, pressure_bogus=1.0)
        with self.assertRaises(ExternalCallViolation):
            external_model_call(decision.build_decision_input(ctx), lambda: "x")


class FallbackAndDeterministicParityTest(unittest.TestCase):
    """The fallback still produces a labelled record; deterministic mode stays
    byte-identical (no derived, no incident, no trace)."""

    def _det(self):
        return decision.DeterministicDecisionProvider(
            playbooks={}, instance_names={VM_ID: "agent-c1"},
            default_instance_name="agent")

    def test_fallback_record_still_generated_with_new_fields_present(self):
        provider = decision.ModelDecisionProvider(
            deterministic=self._det(),
            decide_fn=lambda inp: DecisionError("inference_unavailable", "down"),
            instance_names={VM_ID: "agent-c1"}, default_instance_name="agent")
        result = provider.decide(make_event(), make_context())
        self.assertEqual(result.source, "fallback")
        self.assertIsNotNone(result.incident)
        self.assertEqual(result.incident["generated"], "fallback")
        # evidence is the event's own C0 fields only — no new-field fabrication.
        self.assertEqual(result.incident["evidence"][0]["field"], "ram_used_bytes")

    def test_deterministic_mode_computes_nothing(self):
        det = self._det()
        result = det.decide(make_event(), make_context())
        self.assertEqual(result.source, "deterministic")
        self.assertIsNone(result.incident)
        self.assertIsNone(result.trace)
        self.assertIsNone(result.confidence)

    def test_build_fallback_incident_fills_only_event_c0_fields(self):
        action = RemediationAction(
            kind="instance.restart", params={"instance_name": "agent-c1"},
            reason="oom", playbook="oom-guard-01", rule_id="oom-guard-01")
        rec = decision.build_fallback_incident(make_event(), action, "parse_error")
        self.assertEqual(rec["generated"], "fallback")
        self.assertEqual(len(rec["evidence"]), 1)
        self.assertEqual(rec["evidence"][0]["field"], "ram_used_bytes")


if __name__ == "__main__":
    unittest.main()
