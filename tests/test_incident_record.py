"""Tests for the §4.2 incident record (operator-decision.md, t_957956e5).

Covers the full path of the incident-grade AI analysis record:

* the adapter parses the four additive fields into a ``DecisionResponse``
  (light repair — malformed entries dropped, absent fields are valid);
* guardrail **G9** enforces the contractual bounds/pattern/enum and runs the
  same C2 scan G7 runs over ``rationale`` over every incident string — a C2 hit
  in the title/explanation is a hard fallback, a malformed/C2 entry is dropped
  and recorded;
* ``DecisionResult.incident`` is threaded through the model provider
  (``generated: "model"``) and the §8.1 labelled fallback record
  (``generated: "fallback"``, built only from the fired event's C0 fields);
* the record is persisted in the same statement as the decision and returned
  by ``GET /v1/operator/decisions`` (``opstore.list_decisions``);
* deterministic mode writes no incident columns (§12 byte-identical).

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
from termini.decision.adapter import parse  # noqa: E402
from termini.decision.types import (  # noqa: E402
    DecisionError,
    DecisionResponse,
)

VM_ID = "01M4349RPXWDC52ZDAVMPCMJSH"
NODE_ID = "01M4349RPTT29RNTC6VBCXQ0RC"

INCIDENT = {
    "incident_title": "Memory exhaustion on node 01M4349RPTT29RNTC6VBCXQ0RC",
    "explanation": (
        "ram_used_bytes reached 96.0% of ram_total_bytes and stayed there for "
        "two consecutive samples, and one instance holds 29% of the node's RAM "
        "by itself."
    ),
    "evidence": [
        {"field": "ram_used_bytes", "value": 32641751449, "seq": 1050,
         "ts": "2026-10-04T07:49:05.000Z"},
        {"field": "ram_total_bytes", "value": 34359738368, "seq": 1050},
        {"field": "lxd_instances.ram_used_bytes", "value": 10000000000, "seq": 1050},
    ],
    "actions": [
        {"action_kind": "instance.restart", "target": VM_ID,
         "note": "restart the heaviest instance to reclaim its resident memory"},
    ],
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
        evidence=[{"seq": 1050, "ram_used_bytes": 32641751449}],
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


def model_response(incident=None) -> DecisionResponse:
    resp = DecisionResponse(
        action_id="instance.restart", confidence=0.9,
        rationale="restart the heaviest instance",
        params={"instance_name": "heaviest"},
    )
    if incident is not None:
        resp.incident_title = incident["incident_title"]
        resp.explanation = incident["explanation"]
        resp.evidence = list(incident["evidence"])
        resp.actions = list(incident["actions"])
    return resp


class AdapterIncidentParseTest(unittest.TestCase):
    """The adapter parses + lightly repairs the four additive fields (§6.2)."""

    def test_incident_fields_parsed(self):
        payload = {
            "schema_version": 1,
            "action_id": "instance.restart",
            "params": {"instance_name": "heaviest"},
            "confidence": 0.87,
            "rationale": "reclaim memory",
            **INCIDENT,
        }
        result = parse(json.dumps(payload))
        self.assertIsInstance(result, DecisionResponse)
        self.assertEqual(result.incident_title, INCIDENT["incident_title"])
        self.assertEqual(result.explanation, INCIDENT["explanation"])
        self.assertEqual(result.evidence, INCIDENT["evidence"])
        self.assertEqual(result.actions, INCIDENT["actions"])
        # to_dict() carries them (additive, only when present).
        self.assertEqual(result.to_dict()["evidence"], INCIDENT["evidence"])

    def test_absent_incident_fields_is_valid(self):
        result = parse(json.dumps({
            "schema_version": 1, "action_id": "image.prune", "confidence": 0.5,
            "rationale": "prune",
        }))
        self.assertIsInstance(result, DecisionResponse)
        self.assertEqual(result.incident_title, "")
        self.assertEqual(result.evidence, [])

    def test_malformed_evidence_entry_dropped(self):
        payload = {
            "schema_version": 1, "action_id": "image.prune", "confidence": 0.5,
            "rationale": "prune",
            "evidence": [
                {"field": "ram_used_bytes", "value": 1},
                {"field": "not a valid field name", "value": 2},   # fails pattern
                {"value": 3},                                       # no field
            ],
        }
        result = parse(json.dumps(payload))
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.evidence[0]["field"], "ram_used_bytes")


class GuardrailG9Test(unittest.TestCase):
    """G9 — the contractual bounds/pattern/enum + C2 scan (§7)."""

    def setUp(self):
        self.guardrail = decision.Guardrail()
        self.ctx = make_context()

    def test_valid_record_passes_and_is_labelled_model(self):
        result = self.guardrail.validate(model_response(incident=INCIDENT), self.ctx)
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.incident)
        self.assertEqual(result.incident["generated"], "model")
        self.assertEqual(result.incident["incident_title"], INCIDENT["incident_title"])
        self.assertEqual(result.incident["evidence"], INCIDENT["evidence"])
        self.assertEqual(result.incident["actions"], INCIDENT["actions"])

    def test_no_incident_fields_yields_none(self):
        result = self.guardrail.validate(model_response(), self.ctx)
        self.assertTrue(result.ok)
        self.assertIsNone(result.incident)

    def test_title_clamped_to_bound(self):
        resp = model_response(incident=INCIDENT)
        resp.incident_title = "memory exhaustion on the node " * 40
        result = self.guardrail.validate(resp, self.ctx)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.incident["incident_title"]), 160)

    def test_explanation_clamped_to_bound(self):
        resp = model_response(incident=INCIDENT)
        resp.explanation = "the evidence points to a single instance leak " * 150
        result = self.guardrail.validate(resp, self.ctx)
        self.assertTrue(result.ok)
        self.assertLessEqual(len(result.incident["explanation"]), 2048)

    def test_evidence_clamped_to_max_entries(self):
        resp = model_response(incident=INCIDENT)
        resp.evidence = [
            {"field": f"f{i}", "value": i} for i in range(50)
        ]
        result = self.guardrail.validate(resp, self.ctx)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.incident["evidence"]), 12)

    def test_actions_clamped_to_max_entries(self):
        resp = model_response(incident=INCIDENT)
        resp.actions = [
            {"action_kind": "instance.restart"} for _ in range(20)
        ]
        result = self.guardrail.validate(resp, self.ctx)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.incident["actions"]), 8)

    def test_bad_evidence_field_pattern_dropped_and_recorded(self):
        resp = model_response(incident=INCIDENT)
        resp.evidence = [
            {"field": "ram_used_bytes", "value": 1},
            {"field": "not a field!", "value": 2},
        ]
        result = self.guardrail.validate(resp, self.ctx)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.incident["evidence"]), 1)
        self.assertTrue(any(
            d["rule"] == "G9" and d["decision"] == "drop" and "field" in d["field"]
            for d in result.decisions
        ))

    def test_c2_in_evidence_value_drops_entry(self):
        resp = model_response(incident=INCIDENT)
        resp.evidence = [
            {"field": "ram_used_bytes", "value": 1},
            {"field": "app_logs", "value": "admin@example.com leaked"},  # C2 value
        ]
        result = self.guardrail.validate(resp, self.ctx)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.incident["evidence"]), 1)

    def test_out_of_space_action_kind_dropped(self):
        resp = model_response(incident=INCIDENT)
        resp.actions = [
            {"action_kind": "instance.restart"},
            {"action_kind": "rm -rf /"},
        ]
        result = self.guardrail.validate(resp, self.ctx)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.incident["actions"]), 1)
        self.assertEqual(result.incident["actions"][0]["action_kind"], "instance.restart")

    def test_c2_in_title_is_hard_fallback(self):
        resp = model_response(incident=INCIDENT)
        resp.incident_title = "contact admin@example.com for the incident"
        result = self.guardrail.validate(resp, self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.reject_reason, "schema_violation")

    def test_c2_in_explanation_is_hard_fallback(self):
        resp = model_response(incident=INCIDENT)
        resp.explanation = "leak is from password=secret123 confirmed"
        result = self.guardrail.validate(resp, self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.reject_reason, "schema_violation")

    def test_response_from_dict_reads_incident_fields(self):
        raw = {
            "schema_version": 1, "action_id": "instance.restart",
            "confidence": 0.9, "rationale": "restart",
            "params": {"instance_name": "heaviest"},
            **INCIDENT,
        }
        result = self.guardrail.validate(raw, self.ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.incident["incident_title"], INCIDENT["incident_title"])


class FallbackIncidentTest(unittest.TestCase):
    """The §8.1 labelled fallback record (never dressed up as model output)."""

    def test_build_fallback_incident_shape(self):
        action = RemediationAction(
            kind="instance.restart", params={"instance_name": "agent-c1"},
            reason="oom", playbook="oom-guard-01", rule_id="oom-guard-01",
        )
        rec = decision.build_fallback_incident(make_event(), action, "parse_error")
        self.assertEqual(rec["generated"], "fallback")
        self.assertTrue(rec["incident_title"])
        self.assertIn("oom on node", rec["incident_title"])
        self.assertIn("parse_error", rec["explanation"])
        self.assertIn("instance.restart", rec["explanation"])
        self.assertIn("agent-c1", rec["explanation"])
        # evidence is only the event's own C0 fields — metric + value + seq.
        self.assertEqual(len(rec["evidence"]), 1)
        self.assertEqual(rec["evidence"][0]["field"], "ram_used_bytes")
        self.assertEqual(rec["evidence"][0]["value"], 95.0)
        self.assertEqual(rec["evidence"][0]["seq"], 1050)
        # one action entry, no fabricated outcome.
        self.assertEqual(rec["actions"], [{"action_kind": "instance.restart",
                                           "target": "agent-c1"}])

    def test_provider_fallback_carries_labelled_record(self):
        det = decision.DeterministicDecisionProvider(
            playbooks={"oom": {"type": "oom", "rule_id": "oom-guard-01",
                               "action": {"kind": "instance.restart",
                                          "target": "heaviest", "params": {}},
                               "reason": "oom", "description": "oom"}},
            instance_names={VM_ID: "agent-c1"}, default_instance_name="agent")
        provider = decision.ModelDecisionProvider(
            deterministic=det,
            decide_fn=lambda inp: DecisionError("inference_unavailable", "down"),
            instance_names={VM_ID: "agent-c1"}, default_instance_name="agent")
        result = provider.decide(make_event(), make_context())
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.fallback_reason, "inference_unavailable")
        self.assertIsNotNone(result.incident)
        self.assertEqual(result.incident["generated"], "fallback")
        self.assertIn("inference_unavailable", result.incident["explanation"])


class IncidentPersistenceTest(unittest.TestCase):
    """The record is written in the same statement as the decision and exposed
    by the decisions API; deterministic mode writes no incident columns."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-incident-")
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

    def _run(self, provider):
        cfg = self._cfg()
        lxd = LxdExecutor(self.lxd.base_url, retry_max=2, backoff_s=0.01)
        loop = OperatorLoop(cfg, self.project, detect.Thresholds(), self.playbooks,
                            instance_names={self.vm_id: "agent-c1"},
                            default_instance_name="agent", lxd=lxd,
                            provider=provider)
        self._insert_oom()
        return loop.run_once()

    def test_model_incident_persisted_and_exposed(self):
        det = decision.DeterministicDecisionProvider(
            playbooks=self.playbooks, instance_names={self.vm_id: "agent-c1"})
        provider = decision.ModelDecisionProvider(
            deterministic=det,
            decide_fn=lambda inp: model_response(incident=INCIDENT))
        produced = self._run(provider)
        self.assertEqual(len(produced), 1)

        with db.session(self.db_path) as conn:
            decisions = opstore.list_decisions(conn, 20)
        self.assertEqual(len(decisions), 1)
        row = decisions[0]
        self.assertEqual(row["decision_source"], "model")
        self.assertEqual(row["incident_title"], INCIDENT["incident_title"])
        self.assertEqual(row["incident_explanation"], INCIDENT["explanation"])
        self.assertEqual(row["incident_evidence"], INCIDENT["evidence"])
        self.assertEqual(row["incident_actions"], INCIDENT["actions"])
        self.assertEqual(row["incident_generated"], "model")

    def test_fallback_incident_persisted_and_labelled(self):
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
        self.assertEqual(row["incident_generated"], "fallback")
        self.assertTrue(row["incident_title"])
        self.assertTrue(row["incident_explanation"])
        self.assertIn("inference_unavailable", row["incident_explanation"])
        # evidence[] is event-derived only: metric + value, never a model sentence.
        evidence = row["incident_evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["field"], "ram_used_bytes")

    def test_deterministic_mode_writes_no_incident_columns(self):
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
        self.assertIsNone(row["incident_title"])
        self.assertIsNone(row["incident_explanation"])
        self.assertIsNone(row["incident_evidence"])
        self.assertIsNone(row["incident_actions"])
        self.assertIsNone(row["incident_generated"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
