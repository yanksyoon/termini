"""Assistant API (t_bfd2c2f7) — grounded Q&A with guardrail-admitted execute
authority, offline.

Exercises the real ``AssistantService`` against a real SQLite store, faking only
the two things that would need a live fleet: the model (``model_call``) and the
LXD executor. The HTTP endpoint is wired through the real server in
``AssistantRouteTest``.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER = REPO / "controller"
sys.path.insert(0, str(CONTROLLER))
sys.path.insert(0, str(REPO))

from app import assistant as assistant_mod  # noqa: E402
from app import config, db, errors, events, logs, server as server_mod, store, ulid  # noqa: E402
from app.operator.models import ExecResult  # noqa: E402

PROJECT_ID = "01J0DEM0WARM000000000000AB"
NODE_ID = "01J0DEM0WARM0000000000000N1"
VM_ID = "01M4349RPXWDC52ZDAVMPCMJSH"

COMPUTE_HW = {
    "cpu": {"cores": 8, "threads": 8},
    "memory": {"total_bytes": 32 * 1024 ** 3},
    "gpus": [],
    "disks": [{"size_bytes": 256 * 1024 ** 3}],
}


def clean_sample(seq=1):
    return {
        "seq": seq,
        "ts": "2026-10-17T00:00:00.000Z",
        "uptime_sec": 3600,
        "cpu_util_pct": 31.2,
        "ram_total_bytes": 34_359_738_368,
        "ram_used_bytes": 20_000_000_000,
        "hostname": "0123456789abcdef",
        "ip_local": "10.14.100.0/24",
        "mac_address": "fedcba9876543210",
        "project_id": PROJECT_ID,
        "lxd_instances": [{"vm_id": VM_ID, "state": "running", "ram_used_bytes": 10_000_000_000}],
    }


class FakeExecutor:
    def __init__(self, result="ok"):
        self.result = result
        self.dry_run = False
        self.base_url = "http://stub.lxd"
        self.calls = []

    def execute(self, action):
        self.calls.append(action)
        return ExecResult(result=self.result, detail="executed", method="PUT",
                          path=f"/1.0/instances/{action.params.get('instance_name')}/state",
                          body="{}")

    def with_base_url(self, url):
        return self


def canned_reply(*, answer="answer", citations=None, proposed=None):
    def _call(messages, request_id):
        return json.dumps({
            "answer": answer,
            "citations": citations or [],
            "proposed_actions": proposed or [],
        })
    return _call


class _ServiceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-assistant-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = os.path.join(self.tmp, "controller.db")
        db.migrate(self.db_path)

        self.cfg = SimpleNamespace(
            inference_base_url="http://127.0.0.1:8085",
            lxd_instance_names={},
            lxd_default_instance_name="agent",
            operator_dry_run=False,
        )
        with db.session(self.db_path) as conn:
            store.seed_bootstrap_tokens(conn, ["boot-a"], store.now_iso())
            self.project, _ = store.ensure_project(
                conn, SimpleNamespace(project_id=PROJECT_ID,
                                      project_tokens=["tok"],
                                      max_inference_nodes=1,
                                      new_secret=lambda: "s"), store.now_iso())
            reg = {"registration_id": "node-a", "machine_id": "node-a",
                   "hostname": "node-a", "role_hint": None, "installer_version": None,
                   "hardware_json": COMPUTE_HW, "os": None, "lxd": None}
            self.node_id, _ = store.upsert_node(
                conn, PROJECT_ID, reg, "compute", store.sha256("node-a"), None,
                store.now_iso())

    def _service(self, model_call, executor=None):
        return assistant_mod.AssistantService(
            self.cfg, self.project, executor=executor or FakeExecutor(),
            model_call=model_call,
            logs=logs.OperatorLogs(events.EventBroker(), ring_size=200))

    def _ask(self, service, messages, context=None, conversation_id="conv-1",
             message_id="msg-1"):
        with db.session(self.db_path) as conn:
            return service.ask(conn, messages, context, conversation_id, message_id)

    def _seed_sample(self, sample=None):
        with db.session(self.db_path) as conn:
            conn.execute(
                "INSERT INTO telemetry_samples (call_id, node_id, seq, ts, ts_epoch, payload_json)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("call-1", self.node_id, 1, "2026-10-17T00:00:00.000Z", 0.0,
                 json.dumps(sample or clean_sample())))


class BuildContextTest(_ServiceBase):
    def test_unknown_kind_is_422(self):
        with db.session(self.db_path) as conn:
            with self.assertRaises(errors.ApiError) as cm:
                assistant_mod.build_context(conn, PROJECT_ID, "bogus", "x")
            self.assertEqual(cm.exception.status, 422)

    def test_unknown_id_is_404(self):
        with db.session(self.db_path) as conn:
            with self.assertRaises(errors.ApiError) as cm:
                assistant_mod.build_context(conn, PROJECT_ID, "node", "01MISSING00000000000000")
            self.assertEqual(cm.exception.status, 404)

    def test_none_kind_has_no_records(self):
        with db.session(self.db_path) as conn:
            blob, resolvable = assistant_mod.build_context(conn, PROJECT_ID, "none", None)
        self.assertEqual(blob["records"], [])
        self.assertEqual(resolvable, {})


class ClassAssertionTest(_ServiceBase):
    def test_clean_payload_has_no_violations(self):
        payload = {"schema_version": 1, "request_id": "r",
                   "messages": [{"role": "user", "content": "what is node a?"}],
                   "context": {"kind": "none", "id": None, "records": []}}
        self.assertEqual(assistant_mod.assert_assistant_classes(payload), [])

    def test_c2_field_in_context_is_rejected(self):
        payload = {"schema_version": 1, "request_id": "r",
                   "messages": [{"role": "user", "content": "x"}],
                   "context": {"kind": "node", "id": NODE_ID,
                               "records": [{"app_logs": ["secret"]}]}}
        viol = assistant_mod.assert_assistant_classes(payload)
        self.assertTrue(any("app_logs" in v for v in viol), viol)

    def test_untransformed_c1_in_context_is_rejected(self):
        payload = {"schema_version": 1, "request_id": "r",
                   "messages": [{"role": "user", "content": "x"}],
                   "context": {"kind": "node", "id": NODE_ID,
                               "records": [{"hostname": "prod-db-01"}]}}
        viol = assistant_mod.assert_assistant_classes(payload)
        self.assertTrue(any("hostname" in v for v in viol), viol)


class ModelFailureTest(_ServiceBase):
    def test_model_transport_failure_is_typed_503(self):
        def boom(messages, request_id):
            raise RuntimeError("gemini down")
        service = self._service(boom)
        with db.session(self.db_path) as conn:
            with self.assertRaises(errors.ApiError) as cm:
                service.ask(conn, [{"role": "user", "content": "hi"}], None, "c", "m")
        self.assertEqual(cm.exception.status, 503)
        self.assertEqual(cm.exception.code, "MODEL_UNAVAILABLE")

    def test_unparseable_reply_is_typed_503(self):
        def bad(messages, request_id):
            return "not json at all"
        service = self._service(bad)
        with db.session(self.db_path) as conn:
            with self.assertRaises(errors.ApiError) as cm:
                service.ask(conn, [{"role": "user", "content": "hi"}], None, "c", "m")
        self.assertEqual(cm.exception.status, 503)
        self.assertEqual(cm.exception.code, "MODEL_UNAVAILABLE")


class C2RefusedTest(_ServiceBase):
    def test_c2_context_is_refused_before_the_model_sees_it(self):
        self._seed_sample(dict(clean_sample(), app_logs=["customer payload"]))
        seen = []
        service = self._service(lambda m, rid: seen.append(1) or "x")
        with db.session(self.db_path) as conn:
            with self.assertRaises(errors.ApiError) as cm:
                service.ask(conn, [{"role": "user", "content": "describe node a"}],
                            {"kind": "node", "id": self.node_id}, "c", "m")
        self.assertEqual(cm.exception.status, 422)
        self.assertEqual(cm.exception.code, "GOVERNANCE_REJECT")
        self.assertEqual(seen, [], "the model must never be called")


class ExecutionTest(_ServiceBase):
    def setUp(self):
        super().setUp()
        self._seed_sample(clean_sample())

    def _propose_restart(self, target=VM_ID):
        return canned_reply(answer="restarting the instance",
                            citations=[{"kind": "node", "id": self.node_id, "label": "node-a"}],
                            proposed=[{"action": "instance.restart", "target": target}])

    def test_happy_path_writes_one_audit_and_one_decision(self):
        executor = FakeExecutor("ok")
        service = self._service(self._propose_restart(), executor=executor)
        resp = self._ask(service, [{"role": "user", "content": "restart it"}],
                         {"kind": "node", "id": self.node_id})
        self.assertEqual(resp["executed"][0]["state"], "ok")
        self.assertTrue(resp["executed"][0]["command_id"])
        self.assertTrue(resp["executed"][0]["audit_id"])
        self.assertEqual(len(executor.calls), 1)

        with db.session(self.db_path) as conn:
            audits = conn.execute("SELECT * FROM remediation_audits").fetchall()
            decisions = conn.execute("SELECT * FROM remediation_decisions").fetchall()
            events = conn.execute(
                "SELECT * FROM health_events WHERE type='assistant'").fetchall()
        self.assertEqual(len(audits), 1)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(audits[0]["initiated_by"], "assistant")
        self.assertEqual(audits[0]["conversation_id"], "conv-1")
        self.assertEqual(audits[0]["message_id"], "msg-1")
        self.assertEqual(decisions[0]["initiated_by"], "assistant")
        self.assertEqual(decisions[0]["decision_source"], "assistant")

    def test_idempotent_replay_executes_once(self):
        executor = FakeExecutor("ok")
        service = self._service(self._propose_restart(), executor=executor)
        first = self._ask(service, [{"role": "user", "content": "restart it"}],
                          {"kind": "node", "id": self.node_id})
        second = self._ask(service, [{"role": "user", "content": "restart it"}],
                           {"kind": "node", "id": self.node_id})
        self.assertEqual(first["executed"][0]["state"], "ok")
        self.assertEqual(second["executed"][0]["state"], "already_applied")
        self.assertEqual(second["executed"][0]["audit_id"], first["executed"][0]["audit_id"])
        self.assertEqual(len(executor.calls), 1)
        with db.session(self.db_path) as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM remediation_audits").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_action_outside_allowlist_is_rejected_with_reason(self):
        executor = FakeExecutor("ok")
        service = self._service(canned_reply(
            answer="destroying", proposed=[{"action": "vm.destroy", "target": self.node_id}]),
            executor=executor)
        resp = self._ask(service, [{"role": "user", "content": "destroy it"}],
                         {"kind": "node", "id": self.node_id})
        self.assertEqual(resp["executed"][0]["state"], "rejected")
        self.assertIn("allowlist", resp["executed"][0]["reason"])
        self.assertEqual(executor.calls, [])
        with db.session(self.db_path) as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM remediation_audits").fetchone()["n"]
        self.assertEqual(n, 0)

    def test_unresolvable_target_is_guardrail_rejected(self):
        executor = FakeExecutor("ok")
        service = self._service(self._propose_restart(target="01MISSING00000000000000"),
                                executor=executor)
        resp = self._ask(service, [{"role": "user", "content": "restart it"}],
                         {"kind": "node", "id": self.node_id})
        self.assertEqual(resp["executed"][0]["state"], "rejected")
        self.assertIn(resp["executed"][0]["reason"], ("invalid_target", "target_missing"))
        self.assertEqual(executor.calls, [])

    def test_command_channel_action_issues_command_with_assistant_attribution(self):
        service = self._service(canned_reply(
            answer="draining the node",
            proposed=[{"action": "node.drain", "target": self.node_id}]))
        resp = self._ask(service, [{"role": "user", "content": "drain node a"}],
                         {"kind": "node", "id": self.node_id})
        self.assertEqual(resp["executed"][0]["state"], "queued")
        self.assertTrue(resp["executed"][0]["command_id"])
        self.assertTrue(resp["executed"][0]["audit_id"])

        with db.session(self.db_path) as conn:
            cmd = conn.execute(
                "SELECT type, state, issued_by, node_id FROM commands WHERE command_id = ?",
                (resp["executed"][0]["command_id"],)).fetchone()
            self.assertIsNotNone(cmd)
            self.assertEqual(cmd["type"], "node.drain")
            self.assertEqual(cmd["state"], "queued")   # no pending_approval gate
            self.assertEqual(cmd["issued_by"], "assistant")
            audits = conn.execute(
                "SELECT initiated_by, conversation_id FROM remediation_audits").fetchall()
            self.assertEqual(len(audits), 1)
            self.assertEqual(audits[0]["initiated_by"], "assistant")
            self.assertEqual(audits[0]["conversation_id"], "conv-1")


class CitationAndTruncationTest(_ServiceBase):
    def test_unresolvable_citation_is_surfaced_not_footnoted(self):
        service = self._service(canned_reply(
            answer="fine", citations=[{"kind": "node", "id": "01MISSING00000000000000",
                                       "label": "ghost"}]))
        resp = self._ask(service, [{"role": "user", "content": "hi"}],
                         {"kind": "node", "id": self.node_id})
        self.assertEqual(resp["citations"], [])
        self.assertTrue(any(e["reason"] == "citation_unresolvable" for e in resp["errors"]))

    def test_truncation_is_flagged(self):
        with mock.patch.object(assistant_mod, "CONTEXT_MAX_CHARS", 80):
            service = self._service(canned_reply(answer="ok"))
            resp = self._ask(service, [{"role": "user", "content": "hi"}],
                             {"kind": "node", "id": self.node_id})
        self.assertTrue(resp["truncated"])
        self.assertTrue(any(e["reason"] == "prompt_truncated" for e in resp["errors"]))


class AssistantRouteTest(unittest.TestCase):
    """The HTTP endpoint, wired through the real server with an injected service."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-assistant-http-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = os.path.join(self.tmp, "controller.db")
        db.migrate(self.db_path)

        env = {
            "TERMINI_BOOTSTRAP_TOKEN": "boot-a",
            "TERMINI_PROJECT_TOKEN": "test-project-token",
            "TERMINI_PROJECT_ID": PROJECT_ID,
            "TERMINI_DB_PATH": self.db_path,
            "TERMINI_LXD_BASE_URL": "http://127.0.0.1:8443",
            "TERMINI_HOST": "127.0.0.1",
            "TERMINI_PORT": "0",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            self.cfg = config.Config.load()

        with db.session(self.db_path) as conn:
            store.seed_bootstrap_tokens(conn, list(self.cfg.bootstrap_tokens), store.now_iso())
            self.project, _ = store.ensure_project(conn, self.cfg, store.now_iso())
            reg = {"registration_id": "node-a", "machine_id": "node-a",
                   "hostname": "node-a", "role_hint": None, "installer_version": None,
                   "hardware_json": COMPUTE_HW, "os": None, "lxd": None}
            self.node_id, _ = store.upsert_node(
                conn, PROJECT_ID, reg, "compute", store.sha256("node-a"), None,
                store.now_iso())

        from app import events, inventory, logs, sshca, terminal
        self.broker = events.EventBroker()
        self.logs = logs.OperatorLogs(self.broker, ring_size=200)
        self.assistant = assistant_mod.AssistantService(
            self.cfg, self.project, executor=FakeExecutor("ok"),
            model_call=canned_reply(answer="the node is online",
                                    citations=[{"kind": "node", "id": self.node_id,
                                                "label": "node-a"}]))
        self.srv = server_mod.ControllerServer(
            ("127.0.0.1", 0), self.cfg, self.project, self.broker, self.logs,
            inventory.Inventory.load(None, "ubuntu", 22), terminal.TerminalManager(),
            sshca.SSHCA(None), assistant=self.assistant)
        self.port = self.srv.server_address[1]
        self._thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self._thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _post(self, body, token="test-project-token"):
        import urllib.error
        import urllib.request
        url = f"http://127.0.0.1:{self.port}/v1/projects/{PROJECT_ID}/assistant/ask"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_read_only_question_answered_with_citations(self):
        st, resp = self._post({"messages": [{"role": "user", "content": "is node a up?"}],
                               "context": {"kind": "node", "id": self.node_id}})
        self.assertEqual(st, 200, resp)
        self.assertEqual(resp["answer"], "the node is online")
        self.assertEqual(resp["citations"], [{"kind": "node", "id": self.node_id,
                                              "label": "node-a"}])
        self.assertEqual(resp["decided_by"], "assistant")

    def test_unknown_kind_is_422(self):
        st, resp = self._post({"messages": [{"role": "user", "content": "hi"}],
                               "context": {"kind": "bogus", "id": "x"}})
        self.assertEqual(st, 422, resp)

    def test_missing_token_is_401(self):
        st, resp = self._post({"messages": [{"role": "user", "content": "hi"}]}, token="")
        self.assertEqual(st, 401, resp)


if __name__ == "__main__":
    unittest.main()
