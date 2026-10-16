"""Tests for the model-decision fallback path and the repeated-failure cap.

Covers (operator-decision.md §8, §10):

* every fallback trigger — adapter ``DecisionError`` codes (parse_error,
  schema_violation, inference_timeout, inference_unavailable), guardrail
  rejections (action_not_allowed, invalid_target, low_confidence), a raised
  exception, and a ``None`` adapter response;
* the mandatory deterministic fallback: on any trigger the deterministic
  provider's action is what reaches the executor, with ``source="fallback"``
  and the reason recorded;
* the per-(node, rule) fallback-streak cap and forced-deterministic cooldown;
* end-to-end: the loop records ``decision_source``/``fallback_reason`` in the
  decision log and never blocks remediation on model latency.
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO))

from app import db, store, ulid  # noqa: E402
from app.operator import decision, detect, playbook  # noqa: E402
from app.operator import store as opstore  # noqa: E402
from app.operator import OperatorLoop  # noqa: E402
from app.operator.executor import LxdExecutor  # noqa: E402
from app.operator.models import HealthEvent  # noqa: E402
from stub_lxd import StubLxd  # noqa: E402
from termini.decision.types import DecisionError, DecisionResponse  # noqa: E402

VM_ID = "01M4349RPXWDC52ZDAVMPCMJSH"
NODE_ID = "01M4349RPTT29RNTC6VBCXQ0RC"

PLAYBOOKS = {
    "oom": {
        "type": "oom", "rule_id": "oom-guard-01",
        "action": {"kind": "instance.restart", "target": "heaviest",
                   "params": {"force": True, "timeout_s": 30}},
        "reason": "oom", "description": "oom",
    },
}


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


def make_context() -> decision.DecisionContext:
    insts = [{"vm_id": VM_ID, "state": "running", "ram_used_bytes": 10_000_000_000}]
    return decision.DecisionContext(
        event=make_event(),
        state={"seq": 2, "ram_used_bytes": 32_000_000_000},
        evidence=[{"seq": 1, "lxd_instances": insts}],
        instances=insts,
        instance_names={VM_ID: "agent-c1"},
        default_instance_name="agent",
        allowed_actions=list(decision.ACTION_IDS),
        project_id="01M4349RPSZFTB60AVJEG0ZZY9",
    )


def make_deterministic() -> decision.DeterministicDecisionProvider:
    return decision.DeterministicDecisionProvider(
        playbooks=PLAYBOOKS, instance_names={VM_ID: "agent-c1"},
        default_instance_name="agent",
    )


def good_response() -> DecisionResponse:
    return DecisionResponse(
        action_id="instance.restart", confidence=0.9,
        rationale="restart the heaviest instance",
        params={"instance_name": "heaviest"},
    )


class ModelProviderFallbackTest(unittest.TestCase):
    """The ModelDecisionProvider maps every model-path failure to a fallback."""

    def setUp(self):
        self.det = make_deterministic()
        self.ctx = make_context()

    def _decide(self, decide_fn, **kw):
        provider = decision.ModelDecisionProvider(
            deterministic=self.det, decide_fn=decide_fn,
            instance_names={VM_ID: "agent-c1"}, default_instance_name="agent", **kw)
        return provider.decide(make_event(), self.ctx)

    def test_success_returns_model_source(self):
        result = self._decide(lambda inp: good_response())
        self.assertEqual(result.source, "model")
        self.assertEqual(result.confidence, 0.9)
        self.assertEqual(result.action.kind, "instance.restart")
        self.assertEqual(result.action.params["instance_name"], "agent-c1")
        self.assertIsNone(result.fallback_reason)

    def test_parse_error_falls_back(self):
        result = self._decide(lambda inp: DecisionError("parse_error", "bad json"))
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.fallback_reason, "parse_error")
        self.assertEqual(result.action.kind, "instance.restart")

    def test_schema_violation_falls_back(self):
        result = self._decide(lambda inp: DecisionError("schema_violation", "bad shape"))
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.fallback_reason, "schema_violation")

    def test_inference_timeout_falls_back(self):
        result = self._decide(lambda inp: DecisionError("inference_timeout", "slow"))
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.fallback_reason, "inference_timeout")

    def test_inference_unavailable_falls_back(self):
        result = self._decide(lambda inp: DecisionError("inference_unavailable", "down"))
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.fallback_reason, "inference_unavailable")

    def test_raised_exception_falls_back_unavailable(self):
        def boom(inp):
            raise RuntimeError("kaboom")
        result = self._decide(boom)
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.fallback_reason, "inference_unavailable")

    def test_none_response_falls_back(self):
        result = self._decide(lambda inp: None)
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.fallback_reason, "schema_violation")

    # Guardrail rejections surface as their own fallback reasons.
    def test_out_of_whitelist_action_falls_back_action_not_allowed(self):
        result = self._decide(lambda inp: DecisionResponse(
            action_id="rm -rf /", confidence=0.9, rationale="trust me",
            params={"instance_name": "heaviest"}))
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.fallback_reason, "action_not_allowed")

    def test_low_confidence_falls_back(self):
        result = self._decide(lambda inp: DecisionResponse(
            action_id="instance.restart", confidence=0.1, rationale="restart",
            params={"instance_name": "heaviest"}))
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.fallback_reason, "low_confidence")

    def test_invalid_target_falls_back(self):
        result = self._decide(lambda inp: DecisionResponse(
            action_id="instance.restart", confidence=0.9, rationale="restart",
            params={"instance_name": "nonexistent-vm"}))
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.fallback_reason, "invalid_target")


class FallbackStreakTest(unittest.TestCase):
    """The repeated-failure cap (§10.2): streak, forced cooldown, reset."""

    NOW = "2026-10-04T08:00:00.000Z"

    def test_success_resets_streak(self):
        state, forced = decision.transition_fallback_state(
            {"fallback_streak": 2, "forced_until": None}, "success",
            self.NOW, 3, 300)
        self.assertEqual(state["fallback_streak"], 0)
        self.assertIsNone(state["forced_until"])
        self.assertIsNone(forced)

    def test_fallback_increments_streak(self):
        state, forced = decision.transition_fallback_state(
            {"fallback_streak": 1, "forced_until": None}, "fallback",
            self.NOW, 3, 300)
        self.assertEqual(state["fallback_streak"], 2)
        self.assertIsNone(forced)

    def test_streak_cap_sets_forced_cooldown(self):
        state, forced = decision.transition_fallback_state(
            {"fallback_streak": 2, "forced_until": None}, "fallback",
            self.NOW, 3, 300)
        self.assertEqual(state["fallback_streak"], 0)
        self.assertIsNotNone(state["forced_until"])
        self.assertEqual(forced, state["forced_until"])
        # forced_until is now + cooldown (lexicographically later).
        self.assertGreater(state["forced_until"], self.NOW)

    def test_in_forced_cooldown(self):
        state = {"forced_until": "2026-10-04T08:05:00.000Z"}
        self.assertTrue(decision.in_forced_cooldown(state, "2026-10-04T08:04:00.000Z"))
        self.assertFalse(decision.in_forced_cooldown(state, "2026-10-04T08:05:00.000Z"))
        self.assertFalse(decision.in_forced_cooldown(None, "2026-10-04T08:04:00.000Z"))


class CountingProvider:
    """Always falls back; counts decide() calls so the cooldown skip is visible."""

    def __init__(self, deterministic):
        self.deterministic = deterministic
        self.calls = 0

    def decide(self, event, context):
        self.calls += 1
        base = self.deterministic.decide(event, context)
        return decision.DecisionResult(
            action=base.action, source="fallback", confidence=None,
            fallback_reason="parse_error", guardrail=[],
        )


class LoopDecisionIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-dec-")
        self.db_path = os.path.join(self.tmp, "test.db")
        db.migrate(self.db_path)

        self.lxd = StubLxd()
        self.lxd.start()
        self.addCleanup(self.lxd.stop)
        # The remediation target must exist on the node (see test_operator_loop).
        self.lxd.set_state("agent-c1", "Running")

        with db.session(self.db_path) as conn:
            self.project_id = ulid.new_ulid()
            conn.execute(
                "INSERT INTO projects (id, name, project_token_hash, max_inference_nodes, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (self.project_id, "test", "hash", 1, store.now_iso()))
            self.project = {"id": self.project_id}
            self.node_id = ulid.new_ulid()
            conn.execute(
                "INSERT INTO nodes (id, project_id, registration_id, machine_id,"
                " hostname_hash, role, node_token_hash, hardware_json, os_json,"
                " lxd_json, role_hint, installer_version, status, last_seen_at,"
                " registered_at, source_ip)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.node_id, self.project_id, "reg-1", None,
                 "5f2a9c1e7b4d4a1f", "compute", "tokhash", "{}", None, None,
                 "auto", "0.1.0", "online", store.now_iso(), store.now_iso(), None))

        self.playbooks = playbook.load_playbooks(str(REPO / "controller" / "playbooks"))
        self.vm_id = VM_ID

    def _cfg(self, **overrides):
        base = dict(
            db_path=self.db_path,
            operator_poll_interval_s=1.0,
            operator_cooldown_s=0.0,
            operator_window=60,
            lxd_base_url=self.lxd.base_url,
            operator_dry_run=False,
            lxd_request_timeout_s=5.0,
            lxd_retry_max=2,
            lxd_retry_backoff_s=0.01,
            lxd_client_cert=None,
            lxd_client_key=None,
            operator_decision_mode="model",
            operator_decision_max_fallbacks=3,
            operator_decision_fallback_cooldown_s=300.0,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def _insert_oom(self, seq_start=100):
        with db.session(self.db_path) as conn:
            store.insert_samples(conn, self.node_id, ulid.new_ulid(), [
                {
                    "seq": seq_start + i, "ts": f"2026-10-04T08:00:{i:02d}Z",
                    "ram_total_bytes": 34_359_738_368,
                    "ram_used_bytes": 34_000_000_000,
                    "disk": [{"path": "/dev/sda", "total_bytes": 274_877_906_944,
                              "free_bytes": 200_000_000_000, "write_latency_ms": 3.0,
                              "mount_ro": False}],
                    "net": [{"iface": "eth0", "rx_bytes": 1, "tx_bytes": 1,
                             "latency_ms": 0.8}],
                    "lxd_instances": [
                        {"vm_id": self.vm_id, "state": "running",
                         "ram_used_bytes": 10_000_000_000,
                         "ram_limit_bytes": 16_000_000_000,
                         "disk_used_bytes": 1_000_000_000},
                    ],
                } for i in range(3)
            ])

    def test_model_fallback_records_provenance_and_still_remediates(self):
        det = decision.DeterministicDecisionProvider(
            playbooks=self.playbooks, instance_names={self.vm_id: "agent-c1"})
        provider = decision.ModelDecisionProvider(
            deterministic=det,
            decide_fn=lambda inp: DecisionError("parse_error", "bad json"),
        )
        cfg = self._cfg()
        lxd = LxdExecutor(self.lxd.base_url, retry_max=2, backoff_s=0.01)
        loop = OperatorLoop(cfg, self.project, detect.Thresholds(), self.playbooks,
                            instance_names={self.vm_id: "agent-c1"},
                            default_instance_name="agent", lxd=lxd,
                            provider=provider)
        self._insert_oom()
        produced = loop.run_once()

        self.assertEqual(len(produced), 1)
        self.assertEqual(len(self.lxd.restart_requests()), 1)  # remediation happened

        with db.session(self.db_path) as conn:
            decisions = opstore.list_decisions(conn, 20)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["decision_source"], "fallback")
        self.assertEqual(decisions[0]["fallback_reason"], "parse_error")
        self.assertIsNone(decisions[0]["confidence"])

    def test_fallback_streak_cap_forces_deterministic(self):
        det = decision.DeterministicDecisionProvider(
            playbooks=self.playbooks, instance_names={self.vm_id: "agent-c1"})
        provider = CountingProvider(det)
        cfg = self._cfg()
        lxd = LxdExecutor(self.lxd.base_url, retry_max=2, backoff_s=0.01)
        loop = OperatorLoop(cfg, self.project, detect.Thresholds(), self.playbooks,
                            instance_names={self.vm_id: "agent-c1"},
                            default_instance_name="agent", lxd=lxd,
                            provider=provider)

        event = make_event()
        ctx = decision.DecisionContext(
            event=event,
            state={"seq": 2, "ram_used_bytes": 32_000_000_000},
            evidence=[{"seq": 1, "lxd_instances": [
                {"vm_id": self.vm_id, "ram_used_bytes": 10_000_000_000}]}],
            instances=[{"vm_id": self.vm_id}],
            instance_names={self.vm_id: "agent-c1"},
            default_instance_name="agent",
            allowed_actions=list(decision.ACTION_IDS),
            project_id=self.project_id,
        )
        now = "2026-10-04T08:00:00.000Z"

        # `_decide` owns its own short transactions (the model call must run with
        # none open — see loop.run_once), so it takes no connection.
        for _ in range(3):
            result = loop._decide(self.node_id, event, ctx, now)
            self.assertEqual(result.source, "fallback")
        self.assertEqual(provider.calls, 3)
        # The 4th decision is inside the forced-deterministic cooldown: the
        # model path is skipped entirely and deterministic is used.
        result = loop._decide(self.node_id, event, ctx, now)
        self.assertEqual(result.source, "deterministic")
        self.assertEqual(provider.calls, 3)  # no further model call


if __name__ == "__main__":
    unittest.main(verbosity=2)
