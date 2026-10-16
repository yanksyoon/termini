"""Tests for the §4.3 decision trace (operator-decision.md, t_e07abffc).

Covers the full path of the AI's reasoning-step record:

* the ``DecisionTrace`` builder's caps (≤24 steps, detail ≤512, reply ≤4096 with
  a truncation flag) and its G9 sanitization — a C2 hit in a step ``detail``
  drops the step and records the drop in the guardrail list; a C2 hit in the
  model reply truncates it to ``[dropped: C2]`` with ``state=rejected``;
* the provider's trace: step order (threshold_fired → evidence_window →
  prompt_built → model_call → reply_received → gates → action_decided →
  executed → audit_recorded), the ``decided_by`` label, and the fallback trace
  carrying its reason + rejecting gate;
* persistence: the trace is written in the same statement as the decision and
  returned by ``opstore.list_decisions`` (parsed JSON), with the ``executed`` /
  ``audit_recorded`` steps appended in the audit's transaction;
* deterministic mode writes NO trace columns (§12 byte-identical).

The model is always a stub — no network, no model call.
"""

from __future__ import annotations

import json
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
from app.operator.models import HealthEvent, RemediationAction  # noqa: E402
from stub_lxd import StubLxd  # noqa: E402
from termini.decision.types import (  # noqa: E402
    TRACE_MAX_STEPS,
    TRACE_REPLY_MAX_CHARS,
    TRACE_STEP_DETAIL_MAX_CHARS,
    DecisionError,
    DecisionResponse,
)

VM_ID = "01M4349RPXWDC52ZDAVMPCMJSH"
NODE_ID = "01M4349RPTT29RNTC6VBCXQ0RC"
PROJECT_ID = "01M4349RPSZFTB60AVJEG0ZZY9"

FORWARDED_EGRESS = {
    "backend": "gemini-proxy",
    "model": "deepseek-flash",
    "answered_by": "forwarded-managed-model",
    "destination": "aiplatform.googleapis.com",
    "destination_class": "external",
}


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


def make_context() -> decision.DecisionContext:
    insts = [{"vm_id": VM_ID, "state": "running", "ram_used_bytes": 10_000_000_000}]
    return decision.DecisionContext(
        event=make_event(),
        state={"seq": 2, "ram_used_bytes": 32_000_000_000, "ram_total_bytes": 34_000_000_000},
        evidence=[{"seq": 1, "lxd_instances": insts}],
        instances=insts,
        instance_names={VM_ID: "agent-c1"},
        default_instance_name="agent",
        allowed_actions=list(decision.ACTION_IDS),
        project_id=PROJECT_ID,
    )


def model_response(raw: str | None = None) -> DecisionResponse:
    resp = DecisionResponse(
        action_id="instance.restart", confidence=0.9,
        rationale="restart the heaviest instance",
        params={"instance_name": "heaviest"},
    )
    if raw is not None:
        resp.raw = raw
    resp.usage = {"prompt_tokens": 12, "completion_tokens": 30, "total_tokens": 42}
    return resp


class DecisionTraceBuilderTest(unittest.TestCase):
    """The DecisionTrace builder's caps and G9 sanitization (§4.3)."""

    def test_detail_is_capped_to_512(self):
        trace = decision.DecisionTrace(decided_by="model")
        step = trace.add("threshold_fired", "rule fired", "word " * 300)
        self.assertIsNotNone(step)
        self.assertLessEqual(len(step["detail"]), TRACE_STEP_DETAIL_MAX_CHARS)
        self.assertEqual(len(step["detail"]), TRACE_STEP_DETAIL_MAX_CHARS)

    def test_reply_is_capped_to_4096_and_flagged(self):
        trace = decision.DecisionTrace(decided_by="model")
        step = trace.add_reply("model reply received", "ok " * 1400)
        self.assertEqual(len(step["detail"]), TRACE_REPLY_MAX_CHARS)
        self.assertTrue(step["truncated"])

    def test_reply_not_truncated_when_under_bound(self):
        trace = decision.DecisionTrace(decided_by="model")
        step = trace.add_reply("model reply received", "short")
        self.assertFalse(step["truncated"])
        self.assertEqual(step["detail"], "short")

    def test_max_steps_is_capped_at_24(self):
        trace = decision.DecisionTrace(decided_by="model")
        for i in range(40):
            trace.add("gates", f"gate {i}", f"detail {i}")
        self.assertEqual(len(trace.steps), TRACE_MAX_STEPS)

    def test_c2_in_detail_drops_step_and_records_drop(self):
        trace = decision.DecisionTrace(decided_by="model")
        step = trace.add("threshold_fired", "rule fired", "contact admin@example.com")
        self.assertIsNone(step)
        self.assertNotIn("threshold_fired", [s["step"] for s in trace.steps])
        self.assertTrue(any(
            d["rule"] == "G9" and d["decision"] == "drop" for d in trace.drops
        ))

    def test_c2_in_reply_truncates_to_dropped_c2_and_rejected(self):
        trace = decision.DecisionTrace(decided_by="model")
        step = trace.add_reply("model reply received", '{"note": "admin@example.com"}')
        self.assertIsNotNone(step)
        self.assertEqual(step["detail"], "[dropped: C2]")
        self.assertEqual(step["state"], "rejected")
        self.assertTrue(any(
            d["rule"] == "G9" and d["decision"] == "drop" for d in trace.drops
        ))


class ModelTraceTest(unittest.TestCase):
    """The provider records the ordered trace with the decider labelled (§4.3)."""

    def setUp(self):
        self.det = decision.DeterministicDecisionProvider(
            playbooks={}, instance_names={VM_ID: "agent-c1"},
            default_instance_name="agent")
        self.ctx = make_context()

    def _provider(self, decide_fn, **kw):
        return decision.ModelDecisionProvider(
            deterministic=self.det, decide_fn=decide_fn,
            instance_names={VM_ID: "agent-c1"}, default_instance_name="agent",
            egress_probe=lambda: dict(FORWARDED_EGRESS), **kw)

    def test_model_trace_has_canonical_order_and_named_endpoint(self):
        provider = self._provider(lambda inp: model_response(raw='{"action_id":"instance.restart"}'))
        result = provider.decide(make_event(), self.ctx)
        self.assertEqual(result.source, "model")
        self.assertIsNotNone(result.trace)
        self.assertEqual(result.trace.decided_by, "model")

        steps = result.trace.steps
        order = [s["step"] for s in steps]
        # threshold_fired → evidence_window → prompt_built → model_call →
        # reply_received → gates → action_decided (executed/audit come from the loop).
        self.assertEqual(order[:5], [
            "threshold_fired", "evidence_window", "prompt_built",
            "model_call", "reply_received",
        ])
        self.assertIn("gates", order)
        self.assertIn("action_decided", order[-1])

        model_call = next(s for s in steps if s["step"] == "model_call")
        self.assertIn("endpoint=external", model_call["detail"])
        self.assertIn("answered_by=forwarded-managed-model", model_call["detail"])
        self.assertIn("model=deepseek-flash", model_call["detail"])
        self.assertIn("tokens(prompt=", model_call["detail"])  # usage surfaced

        reply = next(s for s in steps if s["step"] == "reply_received")
        self.assertIn("action_id", reply["detail"])
        self.assertEqual(reply["state"], "ok")

    def test_gates_record_g7_and_g9_accept_on_clean_response(self):
        provider = self._provider(lambda inp: model_response(raw="{}"))
        result = provider.decide(make_event(), self.ctx)
        gates = [s for s in result.trace.steps if s["step"] == "gates"]
        labels = {g["label"] for g in gates}
        self.assertIn("G7 rationale", labels)
        self.assertIn("G9 incident record", labels)

    def test_fallback_trace_carries_reason_and_rejecting_gate(self):
        # low confidence -> guardrail G6 rejects -> fallback with rejecting_gate.
        provider = self._provider(lambda inp: DecisionResponse(
            action_id="instance.restart", confidence=0.1, rationale="restart",
            params={"instance_name": "heaviest"}, raw="{}"))
        result = provider.decide(make_event(), self.ctx)
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.trace.decided_by, "fallback")
        action_step = next(s for s in result.trace.steps if s["step"] == "action_decided")
        self.assertIn("reason=low_confidence", action_step["detail"])
        self.assertIn("rejecting_gate=G6", action_step["detail"])
        self.assertEqual(action_step["state"], "fallback")

    def test_parse_error_fallback_trace(self):
        provider = self._provider(lambda inp: DecisionError("parse_error", "bad json"))
        result = provider.decide(make_event(), self.ctx)
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.trace.decided_by, "fallback")
        action_step = next(s for s in result.trace.steps if s["step"] == "action_decided")
        self.assertIn("reason=parse_error", action_step["detail"])

    def test_c2_in_step_detail_drops_step_and_records_guardrail_drop(self):
        def boom(inp):
            raise RuntimeError("mail admin@example.com now")
        provider = self._provider(boom)
        result = provider.decide(make_event(), self.ctx)
        self.assertEqual(result.source, "fallback")
        # The model_call step (whose detail carries the C2 error) was dropped.
        self.assertNotIn("model_call", [s["step"] for s in result.trace.steps])
        # ...and the drop is recorded in the guardrail list.
        self.assertTrue(any(
            d["rule"] == "G9" and d["decision"] == "drop"
            and "model_call" in d.get("field", "")
            for d in result.guardrail
        ))


class DecisionTracePersistenceTest(unittest.TestCase):
    """The trace is persisted in the same statement as the decision and exposed
    by the decisions API; deterministic mode writes no trace columns."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-trace-")
        self.db_path = os.path.join(self.tmp, "test.db")
        db.migrate(self.db_path)

        self.lxd = StubLxd()
        self.lxd.start()
        self.addCleanup(self.lxd.stop)
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

    def _cfg(self, mode="model"):
        return SimpleNamespace(
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
            operator_decision_mode=mode,
            operator_decision_max_fallbacks=3,
            operator_decision_fallback_cooldown_s=300.0,
        )

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

    def _run(self, provider, mode="model"):
        cfg = self._cfg(mode=mode)
        lxd = LxdExecutor(self.lxd.base_url, retry_max=2, backoff_s=0.01)
        loop = OperatorLoop(cfg, self.project, detect.Thresholds(), self.playbooks,
                            instance_names={self.vm_id: "agent-c1"},
                            default_instance_name="agent", lxd=lxd,
                            provider=provider)
        self._insert_oom()
        return loop.run_once()

    def test_model_trace_persisted_and_exposed(self):
        det = decision.DeterministicDecisionProvider(
            playbooks=self.playbooks, instance_names={self.vm_id: "agent-c1"})
        provider = decision.ModelDecisionProvider(
            deterministic=det,
            decide_fn=lambda inp: model_response(
                raw=json.dumps({"action_id": "instance.restart",
                                "confidence": 0.9,
                                "rationale": "restart the heaviest instance",
                                "params": {"instance_name": "heaviest"}})))
        produced = self._run(provider)
        self.assertEqual(len(produced), 1)

        with db.session(self.db_path) as conn:
            decisions = opstore.list_decisions(conn, 20)
        self.assertEqual(len(decisions), 1)
        row = decisions[0]
        self.assertEqual(row["decision_source"], "model")
        self.assertEqual(row["trace_version"], 1)
        self.assertIsInstance(row["decision_trace"], dict)  # parsed JSON, not a string
        trace = row["decision_trace"]
        self.assertEqual(trace["decided_by"], "model")
        self.assertEqual(trace["trace_version"], 1)

        order = [s["step"] for s in trace["steps"]]
        # The loop appended executed + audit_recorded in the audit transaction.
        self.assertEqual(order[-2:], ["executed", "audit_recorded"])
        self.assertEqual(order[0], "threshold_fired")
        self.assertIn("model_call", order)
        self.assertIn("reply_received", order)

    def test_fallback_trace_persisted_and_labelled(self):
        det = decision.DeterministicDecisionProvider(
            playbooks=self.playbooks, instance_names={self.vm_id: "agent-c1"})
        provider = decision.ModelDecisionProvider(
            deterministic=det,
            decide_fn=lambda inp: DecisionError("inference_unavailable", "down"))
        produced = self._run(provider)
        self.assertEqual(len(produced), 1)

        with db.session(self.db_path) as conn:
            decisions = opstore.list_decisions(conn, 20)
        self.assertEqual(len(decisions), 1)
        row = decisions[0]
        self.assertEqual(row["decision_source"], "fallback")
        trace = row["decision_trace"]
        self.assertIsInstance(trace, dict)
        self.assertEqual(trace["decided_by"], "fallback")
        action_step = next(s for s in trace["steps"] if s["step"] == "action_decided")
        self.assertIn("reason=inference_unavailable", action_step["detail"])

    def test_deterministic_mode_writes_no_trace_columns(self):
        det = decision.DeterministicDecisionProvider(
            playbooks=self.playbooks, instance_names={self.vm_id: "agent-c1"})
        cfg = self._cfg(mode="deterministic")
        lxd = LxdExecutor(self.lxd.base_url, retry_max=2, backoff_s=0.01)
        loop = OperatorLoop(cfg, self.project, detect.Thresholds(), self.playbooks,
                            instance_names={self.vm_id: "agent-c1"},
                            default_instance_name="agent", lxd=lxd,
                            provider=det)
        self._insert_oom()
        produced = loop.run_once()
        self.assertEqual(len(produced), 1)

        with db.session(self.db_path) as conn:
            decisions = opstore.list_decisions(conn, 20)
        self.assertEqual(len(decisions), 1)
        row = decisions[0]
        self.assertEqual(row["decision_source"], "deterministic")
        self.assertIsNone(row["decision_trace"])
        self.assertIsNone(row["trace_version"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
