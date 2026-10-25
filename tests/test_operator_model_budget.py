"""The operator's model-spend guard (operator-decision.md §10.2, amended).

WHAT IS BEING TESTED, AND WHY IT EXISTS
---------------------------------------
The contract caps model calls only after a *fallback* streak, so a fault the
model keeps answering SUCCESSFULLY is bounded by nothing but the per-(node, rule)
cooldown. On the standing fleet a leftover evidence drop-in pinned that cooldown
to 0 while the loop polled every 2 s in `model` mode; Cloud Monitoring measured
42.1M input tokens on 2026-10-18 (2,265 calls, 12 of them HTTP 429) — ~$28 over
two days for work whose useful decisions numbered in the tens.

These tests assert the two things that would have bounded it:

1. `HourlyCallBudget` — a global sliding-hour cap on call ATTEMPTS, which no other
   config value can switch off by accident.
2. The over-budget path still REMEDIATES: it takes the deterministic fast-path,
   so a cap on spend never becomes a cap on self-healing.
3. `evidence_samples` — the window the model sees, which sets the per-call token
   count (20 samples measured 20,601 tokens; 6 measures ~9k).

The window is in-process on purpose (a restart begins a fresh window), and that
limitation is recorded in the contract rather than hidden here.
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

from app import db, store, ulid  # noqa: E402
from app.operator import Thresholds, OperatorLoop  # noqa: E402
from app.operator import decision, playbook  # noqa: E402
from app.operator.budget import HourlyCallBudget  # noqa: E402

RAM_TOTAL = 34_359_738_368
RAM_HIGH = 34_000_000_000


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class HourlyCallBudgetTest(unittest.TestCase):
    def test_unlimited_when_the_limit_is_zero(self):
        budget = HourlyCallBudget(0, clock=FakeClock())
        for _ in range(500):
            self.assertTrue(budget.allows())
            budget.record()
        self.assertFalse(budget.enabled)
        self.assertEqual(budget.remaining(), -1)
        self.assertIn("unlimited", budget.describe())

    def test_a_limit_denies_the_call_after_it(self):
        budget = HourlyCallBudget(3, clock=FakeClock())
        for _ in range(3):
            self.assertTrue(budget.allows())
            budget.record()
        self.assertFalse(budget.allows(), "the 4th attempt must be denied")
        self.assertEqual(budget.used(), 3)
        self.assertEqual(budget.remaining(), 0)

    def test_allows_does_not_count(self):
        """A refusal must not consume budget: otherwise a poll loop drains it."""
        budget = HourlyCallBudget(2, clock=FakeClock())
        for _ in range(10):
            budget.allows()
        self.assertEqual(budget.used(), 0)
        budget.record()
        self.assertEqual(budget.used(), 1)

    def test_the_window_slides(self):
        clock = FakeClock()
        budget = HourlyCallBudget(2, window_s=3600.0, clock=clock)
        budget.record()
        budget.record()
        self.assertFalse(budget.allows())
        self.assertIsNotNone(budget.resume_at())
        clock.advance(3599.0)
        self.assertFalse(budget.allows(), "still inside the hour")
        clock.advance(2.0)
        self.assertTrue(budget.allows(), "the window must free after an hour")
        self.assertIsNone(budget.resume_at())

    def test_budget_is_thread_safe_under_contention(self):
        import threading

        budget = HourlyCallBudget(50, clock=FakeClock())
        admitted = []

        def worker():
            for _ in range(100):
                if budget.allows():
                    budget.record()
                    admitted.append(1)

        threads = [__import__("threading").Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLessEqual(len(admitted), 50, "the cap must hold under contention")
        self.assertLessEqual(budget.used(), 50)


class CountingProvider:
    """A DecisionProvider that records every call and answers 'model'."""

    def __init__(self, action: decision.RemediationAction | None = None) -> None:
        self.calls = 0
        self.action = action

    def decide(self, event, context):
        self.calls += 1
        return decision.DecisionResult(
            action=self.action, source="model", confidence=0.9,
            incident={"generated": "model"},
        )


class OperatorBudgetWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-op-budget-")
        self.db_path = os.path.join(self.tmp, "test.db")
        db.migrate(self.db_path)
        with db.session(self.db_path) as conn:
            self.project_id = ulid.new_ulid()
            conn.execute(
                "INSERT INTO projects (id, name, project_token_hash, max_inference_nodes,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (self.project_id, "test", "hash", 1, store.now_iso()),
            )
        self.project = {"id": self.project_id, "project_token_hash": "hash"}
        self.node_id = ulid.new_ulid()
        self.playbooks = playbook.load_playbooks(
            str(REPO / "controller" / "playbooks"))

    def _loop(self, *, calls_per_hour: int, evidence_samples: int | None = None,
              provider=None):
        cfg = SimpleNamespace(
            db_path=self.db_path,
            operator_poll_interval_s=1.0,
            operator_cooldown_s=0.0,
            operator_window=60,
            operator_decision_mode="model",
            operator_decision_timeout_s=5.0,
            operator_decision_max_fallbacks=3,
            operator_decision_fallback_cooldown_s=300.0,
            operator_model_calls_per_hour=calls_per_hour,
            lxd_base_url="http://127.0.0.1:1",
            operator_dry_run=True,
            lxd_request_timeout_s=1.0,
            lxd_retry_max=0,
            lxd_retry_backoff_s=0.0,
            lxd_client_cert=None,
            lxd_client_key=None,
        )
        if evidence_samples is not None:
            cfg.operator_evidence_samples = evidence_samples
        return OperatorLoop(
            cfg, self.project, Thresholds(), self.playbooks,
            instance_names={}, default_instance_name="agent",
            provider=provider or CountingProvider(),
        )

    def _event_and_context(self, seq: int = 100):
        # The deterministic playbook resolves "heaviest" from the EVENT's evidence
        # window (playbook._heaviest_instance), so the fixture carries one sample
        # with a real instance row — otherwise the fast-path has no target.
        sample = {
            "seq": seq,
            "ram_total_bytes": RAM_TOTAL,
            "ram_used_bytes": RAM_HIGH,
            "lxd_instances": [
                {"vm_id": ulid.new_ulid(), "instance_name": "agent",
                 "state": "running", "ram_used_bytes": 10_000_000_000,
                 "ram_limit_bytes": 16_000_000_000},
            ],
        }
        event = SimpleNamespace(
            rule_id="oom-guard-01", node_id=self.node_id, episode=1, type="oom",
            threshold=">= 95% of ram_total_bytes", metric="ram_used_bytes",
            value=RAM_HIGH, seq=seq, fired_at="2026-10-25T00:00:00Z",
            breach_started_at="2026-10-25T00:00:00Z", evidence=[sample],
        )
        context = decision.DecisionContext(
            event=event, state=sample, evidence=[sample],
            instances=sample["lxd_instances"],
            instance_names={}, default_instance_name="agent",
            allowed_actions=list(decision.ACTION_IDS),
            project_id=self.project_id,
        )
        return event, context

    def test_the_call_budget_denies_a_runaway_loop(self):
        provider = CountingProvider()
        loop = self._loop(calls_per_hour=2, provider=provider)
        event, context = self._event_and_context()

        sources = [loop._decide(self.node_id, event, context, store.now_iso()).source
                   for _ in range(6)]

        self.assertEqual(provider.calls, 2,
                         "only two model calls may be attempted in the window")
        self.assertEqual(sources[:2], ["model", "model"])
        self.assertEqual(sources[2:], ["deterministic"] * 4,
                         "over budget the loop must still decide (deterministic)")

    def test_budget_zero_means_unlimited_and_restores_the_old_behaviour(self):
        provider = CountingProvider()
        loop = self._loop(calls_per_hour=0, provider=provider)
        event, context = self._event_and_context()
        for _ in range(5):
            loop._decide(self.node_id, event, context, store.now_iso())
        self.assertEqual(provider.calls, 5)

    def test_evidence_window_defaults_to_six_and_is_clamped_to_the_contract(self):
        loop = self._loop(calls_per_hour=10)
        self.assertEqual(loop.evidence_samples, 6)
        samples = [{"seq": i} for i in range(60)]

        self.assertEqual(len(loop._build_context(SimpleNamespace(
            rule_id="oom-guard-01"), samples).evidence), 6)

        loop_wide = self._loop(calls_per_hour=10, evidence_samples=500)
        self.assertEqual(loop_wide.evidence_samples, 20,
                         "the contract ceiling is 20 (EVIDENCE_SAMPLE_CAP)")
        self.assertEqual(
            len(loop_wide._build_context(SimpleNamespace(
                rule_id="oom-guard-01"), samples).evidence), 20)

        loop_narrow = self._loop(calls_per_hour=10, evidence_samples=0)
        self.assertEqual(loop_narrow.evidence_samples, 1,
                         "0 is clamped up to 1, never an empty window")

    def test_the_over_budget_path_still_remediates(self):
        """A spend cap must never become a cap on self-healing."""
        action = decision.RemediationAction(
            kind="instance.restart", params={"instance_name": "heaviest"})
        provider = CountingProvider(action=action)
        loop = self._loop(calls_per_hour=1, provider=provider)
        event, context = self._event_and_context()

        loop._decide(self.node_id, event, context, store.now_iso())     # model
        result = loop._decide(self.node_id, event, context, store.now_iso())
        self.assertEqual(result.source, "deterministic")
        self.assertIsNotNone(result.action,
                             "the deterministic playbook must still choose an action")


if __name__ == "__main__":
    unittest.main()
