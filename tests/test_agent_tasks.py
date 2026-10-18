"""Agent task API (t_1a2c913c) — the controller half, end to end and offline.

`POST /v1/projects/{pid}/agents/{vm_id}/tasks` is the contract addition: a client
hands a task to one of its agents through the controller and reads the answer back,
with the submission audited like a node action (agent-runtime.md §5.6/§7.4).

The end-to-end test here wires the REAL pieces against each other and fakes only
the two things that would need a live fleet:

  * the node's LXD REST API is `tests/stub_lxd.StubLxd`, now with the exec surface
    (`POST /1.0/instances/{n}/exec` + `record-output`, exactly the shape measured
    on Node A: `metadata.output` + `metadata.return`, served back via
    `/1.0/instances/{n}/logs/...`);
  * the *inside of the instance* is the REAL in-VM task surface —
    `termini_agent.taskapi.TaskService` behind the REAL `StatusServer` on
    127.0.0.1 — so the request the stub executes is the request the shipped
    runtime would serve;
  * the model is faked at the inference client, so no endpoint is contacted.

What that proves without a fleet: the controller resolves the agent, audits the
submission, reaches the agent's loopback surface through the node, follows the task
to a terminal state, returns the answer with the endpoint's provenance label, and
files the failure codes distinctly (bound exceeded, retired alias, unreachable
agent, unknown agent).
"""
from __future__ import annotations

import json
import os
import pathlib
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER = REPO / "controller"
sys.path.insert(0, str(CONTROLLER))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "image" / "agent-vm" / "agent" / "src"))

from app import agent_tasks, config, db, events, inventory, logs, server as server_mod, sshca, store, terminal  # noqa: E402
from stub_lxd import StubLxd  # noqa: E402

PROJECT_ID = "01J0DEM0WARM000000000000AB"
PROJECT_TOKEN = "test-project-token"
VM = "termini-01m4wjhwqr3b8pcav2tvqbgg-0-0"
GiB = 1024 ** 3

COMPUTE_HW = {
    "cpu": {"cores": 8, "threads": 8},
    "memory": {"total_bytes": 32 * GiB},
    "gpus": [],
    "disks": [{"size_bytes": 256 * GiB}],
}

AGENT_YAML = """
project_id: 01J0DEM0WARM000000000000AB
vm_id: 01J00000000000000000000001
node_id: 01J00000000000000000000002
agent_image_version: agent-vm-2026.10.0
inference:
  base_url: http://10.10.0.13:8080
  default_model: gemma
  allowed_models: [gemma]
broker:
  endpoint: http://10.20.1.1:7071
  max_concurrent_tasks: 2
logging:
  sink: /tmp/agent-mount-test.jsonl
tasks:
  max_concurrent: 2
"""

PROVENANCE = {
    "backend": "gemini-proxy",
    "answered_by": "forwarded-managed-model",
    "destination": "aiplatform.googleapis.com",
    "forwarded": True,
}


class FakeInference:
    """Stands in for the endpoint: an answer, or a chosen failure."""

    def __init__(self, *, behaviour="ok", content="pong", release=None):
        self.behaviour = behaviour
        self.content = content
        self.release = release
        self.allowed = ("gemma",)

    def resolve_model(self, preference):
        from termini_agent.inference import ModelNotAllowed

        model = preference or self.allowed[0]
        if model not in self.allowed:
            raise ModelNotAllowed(f"model {model!r} not in {list(self.allowed)}")
        return model

    def complete_with_provenance(self, messages, *, model=None, max_tokens=None,
                                 temperature=None):
        from termini_agent.inference import (Completion, EmptyCompletion,
                                             InferenceUnreachable)

        if self.release is not None:
            self.release.wait(10)
        if self.behaviour == "unreachable":
            raise InferenceUnreachable(
                "inference endpoint http://10.10.0.13:8080 is unreachable: "
                "All connection attempts failed")
        if self.behaviour == "empty":
            raise EmptyCompletion("inference endpoint returned HTTP 200 with empty content")
        return Completion(content=self.content, model="gemma", request_id="01M0REQ",
                          provenance=dict(PROVENANCE),
                          usage={"prompt_tokens": 5, "completion_tokens": 1,
                                 "total_tokens": 6})


class AgentMount:
    """A REAL in-VM task surface (StatusServer + TaskService) on loopback."""

    def __init__(self, inference=None, bound=2):
        from termini_agent.config import load_config
        from termini_agent.status import StatusServer, StatusState
        from termini_agent.taskapi import TaskService

        d = pathlib.Path(tempfile.mkdtemp(prefix="agent-mount-"))
        cfg_path = d / "agent.yaml"
        cfg_path.write_text(AGENT_YAML, encoding="utf-8")
        cfg = load_config(cfg_path, dev_override=False)
        self.service = TaskService(cfg, client=inference or FakeInference(),
                                   bound=bound, result_sink=str(d / "results"))
        self.state = StatusState({"kind": "termini.agent_status", "state": "idle",
                                  "tasks": self.service.counters()})
        self.server = StatusServer(self.state, http_port=0, unix_path=None,
                                   tasks=self.service)
        self.server.start()
        self.tmp = d

    @property
    def port(self) -> int:
        return self.server.port

    def stop(self):
        self.server.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


def _load_cfg(db_path: str, lxd_base: str):
    env = {
        "TERMINI_BOOTSTRAP_TOKEN": "boot-a",
        "TERMINI_PROJECT_TOKEN": PROJECT_TOKEN,
        "TERMINI_PROJECT_ID": PROJECT_ID,
        "TERMINI_DB_PATH": db_path,
        "TERMINI_LXD_BASE_URL": lxd_base,
        "TERMINI_LXD_CREATE_TIMEOUT_S": "10",
        "TERMINI_HOST": "127.0.0.1",
        "TERMINI_PORT": "0",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        return config.Config.load()


def _agent_exec_handler(mount: AgentMount):
    """The instance's own behaviour: run the shipped remote client's argv.

    `agent_tasks.REMOTE_CLIENT` is invoked as
    `python3 -c <script> <method> <url> <timeout> [payload]`; real LXD would run it
    inside the instance, where 127.0.0.1:9101 is the agent. Here the same argv is
    honoured against the mount's real HTTP surface, with the port rewritten because
    the test's agent is on an ephemeral port rather than the frozen 9101.
    """

    def handler(argv: list[str]) -> tuple[int, str, str]:
        if len(argv) < 5 or argv[1] != "-c":
            return 2, "", "unexpected argv"
        method, url, timeout = argv[3], argv[4], float(argv[5])
        payload = argv[6] if len(argv) > 6 else None
        url = url.replace(f":{agent_tasks.AGENT_SURFACE_PORT}", f":{mount.port}")
        data = payload.encode() if payload else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return 0, json.dumps({"status": resp.status,
                                      "body": resp.read().decode(),
                                      "retry_after": resp.headers.get("Retry-After")}), ""
        except urllib.error.HTTPError as exc:
            return 0, json.dumps({"status": exc.code,
                                  "body": exc.read().decode(),
                                  "retry_after": (exc.headers.get("Retry-After")
                                                  if exc.headers else None)}), ""
        except Exception as exc:  # noqa: BLE001 — the client's own failure shape
            return 0, json.dumps({"status": 0, "body": json.dumps(
                {"error": f"{type(exc).__name__}: {exc}"})}), ""

    return handler


class _Base(unittest.TestCase):
    lxd_endpoint = None          # a reported endpoint would bypass the stub fallback

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-agenttask-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = os.path.join(self.tmp, "controller.db")
        db.migrate(self.db_path)

        self.lxd = StubLxd()
        self.lxd.start()
        self.addCleanup(self.lxd.stop)
        self.lxd.instances[VM] = {"name": VM}
        self.lxd.set_state(VM, "Running")

        self.cfg = _load_cfg(self.db_path, self.lxd.base_url)
        with db.session(self.db_path) as conn:
            store.seed_bootstrap_tokens(conn, list(self.cfg.bootstrap_tokens), store.now_iso())
            self.project, _ = store.ensure_project(conn, self.cfg, store.now_iso())
            reg = {"registration_id": "compute-1", "machine_id": "compute-1",
                   "hostname": "compute-1", "role_hint": None,
                   "installer_version": None, "hardware_json": COMPUTE_HW,
                   "os": None, "lxd": None, "lxd_endpoint": self.lxd_endpoint}
            self.node_id, _ = store.upsert_node(
                conn, PROJECT_ID, reg, "compute", store.sha256("compute-1"), None,
                store.now_iso())
            store.commit_allocation(
                conn, PROJECT_ID, "PLAN000000000000000000000A", self.node_id,
                cpu_allocated=2, ram_allocated_gb=4, disk_allocated_gb=8,
                now=store.now_iso())
            store.set_allocation_instances(
                conn, PROJECT_ID, "PLAN000000000000000000000A", self.node_id, [VM])

        self.broker = events.EventBroker()
        self.logs = logs.OperatorLogs(self.broker, ring_size=200)
        self.srv = server_mod.ControllerServer(
            ("127.0.0.1", 0), self.cfg, self.project, self.broker, self.logs,
            inventory.Inventory.load(None, "ubuntu", 22), terminal.TerminalManager(),
            sshca.SSHCA(None),
        )
        self.port = self.srv.server_address[1]
        self._thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self._thread.start()
        self.addCleanup(self._stop)

        self.mount = AgentMount()
        self.addCleanup(self.mount.stop)
        self.lxd.exec_handler = _agent_exec_handler(self.mount)

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    # -- plumbing ---------------------------------------------------------------
    def _call(self, method, path, body=None, token=PROJECT_TOKEN):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return e.code, {}

    def _post_task(self, body, vm=VM):
        return self._call("POST", f"/v1/projects/{PROJECT_ID}/agents/{vm}/tasks", body)

    def _post_task_with_headers(self, body, vm=VM):
        """POST a task and return (status, body, response headers)."""
        url = f"http://127.0.0.1:{self.port}/v1/projects/{PROJECT_ID}/agents/{vm}/tasks"
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + PROJECT_TOKEN})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), dict(e.headers)

    def _get_task(self, task_id, vm=VM):
        return self._call("GET", f"/v1/projects/{PROJECT_ID}/agents/{vm}/tasks/{task_id}")

    def _wait(self, task_id, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st, task = self._get_task(task_id)
            if st == 200 and task["state"] in ("done", "error"):
                return task
            time.sleep(0.2)
        raise AssertionError(f"task {task_id} never finished: {self._get_task(task_id)}")


class AgentTaskRouteTest(_Base):
    def test_submit_returns_the_answer_with_provenance_and_is_audited(self):
        st, task = self._post_task({"prompt": "Reply with exactly: pong",
                                    "actor": "human:operator"})
        self.assertEqual(st, 202, task)
        self.assertEqual(task["vm_id"], VM)
        self.assertEqual(task["state"], "queued")
        self.assertTrue(task["command_id"])
        task_id = task["task_id"]

        done = self._wait(task_id)
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["answer"], "pong")
        self.assertEqual(done["model"], "gemma")
        # Honest provenance: the endpoint's own label travels with the answer.
        self.assertEqual(done["provenance"]["backend"], "gemini-proxy")
        self.assertTrue(done["provenance"]["forwarded"])

        # The submission went through the node's LXD as an exec inside the instance.
        execs = self.lxd.exec_requests(VM)
        self.assertGreaterEqual(len(execs), 2)   # the submit + at least one poll
        self.assertEqual(execs[0][2]["command"][0], "/usr/bin/python3")

        # Audited exactly like a node action: a command row + a content-free log line.
        with db.session(self.db_path) as conn:
            cmd = conn.execute("SELECT type, state, issued_by, node_id FROM commands"
                               " WHERE command_id = ?", (task["command_id"],)).fetchone()
            self.assertIsNotNone(cmd)
            self.assertEqual(cmd["type"], "agent.dispatch_task")
            self.assertEqual(cmd["issued_by"], "human")
            self.assertEqual(cmd["node_id"], self.node_id)
            self.assertEqual(cmd["state"], "succeeded")

        # The completion line is written by the poller, just after the record flips.
        deadline = time.time() + 5
        lines: list = []
        while time.time() < deadline:
            with db.session(self.db_path) as conn:
                lines = conn.execute("SELECT actor, message, severity FROM operator_logs"
                                     " ORDER BY log_id").fetchall()
            if any(f"agent task {task_id}" in ln["message"] for ln in lines):
                break
            time.sleep(0.1)
        messages = [ln["message"] for ln in lines]
        self.assertTrue(any("agent.dispatch_task issued" in m for m in messages), messages)
        self.assertTrue(any(f"agent task {task_id}" in m and "done" in m for m in messages),
                        messages)
        for m in messages:
            self.assertNotIn("Reply with exactly", m)   # never the prompt
            self.assertFalse(m.endswith("done (model gemma") and "pong" in m,
                             "the log line must not carry the answer")
        self.assertEqual([ln["actor"] for ln in lines][0], "human")

    def test_task_and_result_are_enumerable(self):
        st, task = self._post_task({"prompt": "one"})
        self.assertEqual(st, 202)
        self._wait(task["task_id"])
        st, listing = self._call(
            "GET", f"/v1/projects/{PROJECT_ID}/agents/{VM}/tasks")
        self.assertEqual(st, 200, listing)
        self.assertEqual(listing["count"], 1)
        row = listing["tasks"][0]
        self.assertEqual(row["state"], "done")
        # A listing is not the content read path.
        self.assertNotIn("answer", row)

    def test_bound_refusal_is_distinct_and_actionable(self):
        """The agent is the resource owner: its bound is what refuses, with 429."""
        release = threading.Event()
        self.mount.service.bound = 1
        self.mount.service._client = FakeInference(release=release)
        st, first = self._post_task({"prompt": "slow"})
        self.assertEqual(st, 202, first)
        st, body, headers = self._post_task_with_headers({"prompt": "second"})
        self.assertEqual(st, 429, body)
        self.assertEqual(body["error"]["code"], "TASK_BOUND_EXCEEDED")
        self.assertTrue(body["error"]["details"][0]["reason"])
        # §5.6/§4.9 promise `Retry-After` on the bound refusal; the agent sends
        # it and the proxy must not swallow it.
        self.assertEqual(headers.get("Retry-After"), "5", headers)
        release.set()
        self._wait(first["task_id"])
        # ... and the refusal is on the record, not just in the response.
        st, listing = self._call("GET", f"/v1/projects/{PROJECT_ID}/agents/{VM}/tasks")
        codes = [t["error_code"] for t in listing["tasks"]]
        self.assertIn("TASK_BOUND_EXCEEDED", codes)

    def test_retired_alias_is_refused_not_substituted(self):
        st, body = self._post_task({"prompt": "x", "model_preference": "deepseek-flash"})
        self.assertEqual(st, 400, body)
        self.assertEqual(body["error"]["code"], "MODEL_NOT_ALLOWED")
        self.assertIn("allowed_models", json.dumps(body))
        # The refusal names the field the caller got wrong, not `vm_id`.
        self.assertEqual(body["error"]["details"][0]["field"], "model_preference")

    def test_unreachable_model_endpoint_is_a_distinct_error(self):
        self.mount.service._client = FakeInference(behaviour="unreachable")
        st, task = self._post_task({"prompt": "x"})
        self.assertEqual(st, 202, task)
        done = self._wait(task["task_id"])
        self.assertEqual(done["state"], "error")
        self.assertEqual(done["error_code"], "INFERENCE_UNREACHABLE")
        self.assertIn("node C", done["error"]["actionable"])
        self.assertIsNone(done["answer"])

    def test_empty_completion_is_not_a_success(self):
        self.mount.service._client = FakeInference(behaviour="empty")
        st, task = self._post_task({"prompt": "x"})
        done = self._wait(task["task_id"])
        self.assertEqual(done["state"], "error")
        self.assertEqual(done["error_code"], "EMPTY_COMPLETION")

    def test_unknown_agent_is_404_with_the_known_ids(self):
        st, body = self._post_task({"prompt": "x"}, vm="termini-nope-0-0")
        self.assertEqual(st, 404, body)
        self.assertEqual(body["error"]["code"], "AGENT_NOT_FOUND")
        details = body["error"]["details"][0]
        self.assertEqual(details["reason"], "unknown")
        # The client-safe derived id, never the internal instance name.
        self.assertIn(agent_tasks.agent_id_for_name(VM), details["known_agent_ids"])
        self.assertNotIn(VM, json.dumps(details))
        # The 404 is actionable: the forms compared and the sources consulted.
        self.assertEqual(details["literal_name"], "termini-nope-0-0")
        self.assertEqual(details["derived_agent_id"],
                         agent_tasks.agent_id_for_name("termini-nope-0-0"))
        self.assertIn("node_telemetry", details["sources"])
        self.assertEqual(details["unscanned_nodes"], [])

    def test_agent_without_the_task_surface_is_unreachable_not_silent(self):
        """An instance whose runtime predates §5.6 must fail loudly, never hang."""
        self.lxd.exec_handler = lambda argv: (0, json.dumps(
            {"status": 0, "body": json.dumps({"error": "URLError: connection refused"})}), "")
        st, body = self._post_task({"prompt": "x"})
        self.assertEqual(st, 503, body)
        self.assertEqual(body["error"]["code"], "AGENT_UNREACHABLE")
        self.assertIn("127.0.0.1", body["error"]["message"])

    def test_prompt_bound_and_auth(self):
        st, body = self._post_task({"prompt": "a" * 9000})
        self.assertEqual(st, 413, body)
        self.assertEqual(body["error"]["code"], "PROMPT_TOO_LARGE")

        st, body = self._post_task({"prompt": "x"})
        self.assertNotIn((body.get("error") or {}).get("code"), ("UNAUTHENTICATED",))
        st, body = self._call("POST", f"/v1/projects/{PROJECT_ID}/agents/{VM}/tasks",
                              {"prompt": "x"}, token="wrong-token")
        self.assertEqual(st, 403, body)

    def test_offline_node_is_refused_before_dispatch(self):
        with db.session(self.db_path) as conn:
            conn.execute("UPDATE nodes SET status = 'offline', last_seen_at = ?",
                         ("2020-01-01T00:00:00Z",))
            conn.commit()
        st, body = self._post_task({"prompt": "x"})
        self.assertEqual(st, 503, body)
        self.assertEqual(body["error"]["code"], "NODE_OFFLINE")
        self.assertEqual(self.lxd.exec_requests(VM), [])


class ObservedByTheClientTest(_Base):
    """The two read paths a UI uses once a task is running.

    Required work #2 (progress a UI can render) and #4/(c) (audited and retrievable
    through the controller API) — read through the client's own surfaces rather than
    the database: `/stream/v1` relays the `EventBroker` envelope verbatim, and the
    operator-log lines are replayed on the SSE log stream the dashboard consumes.
    """

    def test_event_stream_renders_queued_running_done_and_never_carries_content(self):
        release = threading.Event()
        self.mount.service._client = FakeInference(release=release)
        sub = self.broker.subscribe()
        self.addCleanup(self.broker.unsubscribe, sub)

        st, task = self._post_task({"prompt": "Reply with exactly: pong"})
        self.assertEqual(st, 202, task)
        task_id = task["task_id"]

        def collect(until_state: str) -> list[dict]:
            seen: list[dict] = []
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    msg = sub.get(timeout=0.5)
                except queue.Empty:
                    continue
                if not str(msg.get("event", "")).startswith("task."):
                    continue
                seen.append(msg)
                if (msg.get("data") or {}).get("state") == until_state:
                    return seen
            return seen

        # The model call is parked, so the task is genuinely `running` when polled.
        events = collect("running")
        self.assertIn("task.queued", [e["event"] for e in events], events)
        self.assertTrue(any(e["event"] == "task.state_changed"
                            and e["data"]["state"] == "running" for e in events), events)
        self.assertEqual(events[0]["event"], "task.queued")

        release.set()
        events += collect("done")
        self.assertEqual(events[-1]["event"], "task.completed")
        self.assertEqual(events[-1]["data"]["state"], "done")
        self.assertEqual(events[-1]["data"]["model"], "gemma")
        self.assertIn("duration_ms", events[-1]["data"])

        for e in events:
            self.assertEqual(e["data"]["task_id"], task_id)
            self.assertEqual(e["data"]["vm_id"], VM)
            blob = json.dumps(e)
            self.assertNotIn("Reply with exactly", blob)   # never the prompt
            self.assertNotIn("pong", blob)                 # never the answer

        done = self._get_task(task_id)[1]
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["answer"], "pong")

    def test_submission_is_human_audited_and_readable_on_the_client_log_stream(self):
        lines: list[dict] = []
        stop = threading.Event()

        def reader():
            url = (f"http://127.0.0.1:{self.port}/v1/projects/{PROJECT_ID}/logs"
                   f"?token={PROJECT_TOKEN}")
            try:
                # The controller polls the agent every 2 s, so the gap between the
                # dispatch line and the completion line is a couple of seconds of
                # silence on the wire: the read timeout must sit above that, not
                # below it, or the reader gives up mid-task.
                with urllib.request.urlopen(urllib.request.Request(url),
                                            timeout=8.0) as resp:
                    for raw in resp:
                        if stop.is_set():
                            break
                        text = raw.decode("utf-8", "replace").strip()
                        if text.startswith("data: "):
                            lines.append(json.loads(text[len("data: "):]))
            except Exception:  # noqa: BLE001 — a closed/slow stream is not the assertion
                return

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        deadline = time.time() + 5
        while time.time() < deadline and self.broker.subscriber_count < 1:
            time.sleep(0.02)
        self.assertGreaterEqual(self.broker.subscriber_count, 1,
                                "the log stream never subscribed")

        st, task = self._post_task({"prompt": "Reply with exactly: pong",
                                    "actor": "human:ops"})
        self.assertEqual(st, 202, task)
        task_id = task["task_id"]
        self._wait(task_id)

        deadline = time.time() + 10
        while time.time() < deadline:
            if any(f"agent task {task_id}" in (ln.get("message") or "") for ln in lines):
                break
            time.sleep(0.1)
        stop.set()
        thread.join(timeout=3)

        messages = [ln.get("message", "") for ln in lines]
        self.assertTrue(any("agent.dispatch_task issued" in m for m in messages), messages)
        self.assertTrue(any(f"agent task {task_id}" in m and "done" in m
                            for m in messages), messages)
        audit = [ln for ln in lines
                 if "agent task" in (ln.get("message") or "")
                 or "dispatch_task" in (ln.get("message") or "")]
        self.assertTrue(audit, lines)
        self.assertEqual({ln["actor"] for ln in audit}, {"human"}, audit)
        for m in messages:
            self.assertNotIn("Reply with exactly", m)   # never the prompt
            self.assertNotIn("pong", m)                 # never the answer


class ResolveAgentTest(_Base):
    def test_resolves_from_the_live_allocation(self):
        with db.session(self.db_path) as conn:
            node, reason, known = agent_tasks.resolve_agent(
                conn, self.cfg, PROJECT_ID, VM)
        self.assertIsNone(reason)
        self.assertEqual(node["id"], self.node_id)
        self.assertIn(VM, known)

    def test_resolves_an_instance_the_allocation_does_not_mention(self):
        """Day-2/adopted instances: `vm_id` IS the LXD instance name."""
        other = "termini-somewhere-else-0-0"
        self.lxd.instances[other] = {"name": other}
        with db.session(self.db_path) as conn:
            node, reason, _ = agent_tasks.resolve_agent(
                conn, self.cfg, PROJECT_ID, other)
        self.assertIsNone(reason)
        self.assertEqual(node["id"], self.node_id)

    def test_unknown_agent_reports_unknown(self):
        with db.session(self.db_path) as conn:
            node, reason, _ = agent_tasks.resolve_agent(
                conn, self.cfg, PROJECT_ID, "termini-nope-0-0")
        self.assertIsNone(node)
        self.assertEqual(reason, "unknown_vm")


class AgentResolutionHonestyTest(_Base):
    """The three honest classifications, distinct from one another.

    The defect this fixes answered "no such agent" (404) for an id whose owning
    node simply could not be enumerated. These tests pin the three outcomes:

      * a telemetry-only id is attributed to the node the client was shown;
      * a node whose instance list cannot be read turns an otherwise-unknown id
        into a 503 that NAMES the unreadable node — never a 404;
      * a genuinely unknown id (every node read, id in none) is still a 404.
    """

    def _seed_telemetry(self, derived: str) -> None:
        with db.session(self.db_path) as conn:
            store.insert_samples(conn, self.node_id, "telemetry-call", [
                {"seq": 1, "ts": "2026-10-18T05:00:00Z",
                 "lxd_instances": [{"vm_id": derived, "state": "running"}]}])
            conn.commit()

    def test_id_only_in_telemetry_is_attributed_to_its_node(self):
        derived = agent_tasks.agent_id_for_name("demo-workload")
        self._seed_telemetry(derived)
        with db.session(self.db_path) as conn:
            res = agent_tasks.resolve_agent_target(conn, self.cfg, PROJECT_ID, derived)
        # Telemetry names the owner but is not dispatch authority: the instance
        # is not seen live, so it must never be tasked.
        self.assertIsNone(res.node)
        self.assertEqual(res.telemetry_node_ids, [self.node_id])
        self.assertIn("node_telemetry", res.sources)
        # The node IS readable and its live list does not contain the id, so the
        # honest answer is still "not found" — but attributed, not anonymous.
        self.assertEqual(res.reason, "unknown_vm")

    def test_unscannable_node_makes_unknown_id_503_not_404(self):
        with db.session(self.db_path) as conn:
            conn.execute("UPDATE nodes SET lxd_endpoint = ?", ("http://127.0.0.1:1",))
            conn.commit()
        st, body = self._post_task({"prompt": "x"}, vm="termini-nope-0-0")
        self.assertEqual(st, 503, body)
        self.assertEqual(body["error"]["code"], "NODE_UNREACHABLE")
        details = body["error"]["details"][0]
        self.assertEqual(details["reason"], "node_unreachable")
        self.assertEqual([u["node_id"] for u in details["unscanned_nodes"]],
                         [self.node_id])

    def test_telemetry_attributed_id_on_unscannable_node_is_503_naming_it(self):
        derived = agent_tasks.agent_id_for_name("demo-workload")
        self._seed_telemetry(derived)
        with db.session(self.db_path) as conn:
            conn.execute("UPDATE nodes SET lxd_endpoint = ?", ("http://127.0.0.1:1",))
            conn.commit()
        st, body = self._post_task({"prompt": "x"}, vm=derived)
        self.assertEqual(st, 503, body)
        self.assertEqual(body["error"]["code"], "NODE_UNREACHABLE")
        details = body["error"]["details"][0]
        # The unreadable node is named, and telemetry names the owner.
        self.assertEqual([u["node_id"] for u in details["unscanned_nodes"]],
                         [self.node_id])
        self.assertEqual(details["attributed_nodes"], [self.node_id])


class TelemetryIdTaskTest(_Base):
    """The client only ever sees `lxd_instances[].vm_id` (a one-way hash).

    `POST .../agents/{vm_id}/tasks` must therefore address the instance by that
    id, while the proxy execs into the real instance name. Without this the
    Agents panel could list an agent and still 404 when you give it a task.
    """

    def test_submit_with_the_derived_telemetry_id_dispatches_by_instance_name(self):
        derived = agent_tasks.agent_id_for_name(VM)
        self.assertNotEqual(derived, VM)
        st, task = self._call(
            "POST", f"/v1/projects/{PROJECT_ID}/agents/{derived}/tasks",
            {"prompt": "Reply with exactly: pong", "actor": "human:operator"})
        self.assertEqual(st, 202, task)
        self.assertEqual(task["vm_id"], derived)

        deadline = time.time() + 15.0
        while time.time() < deadline:
            st_get, got = self._get_task(task["task_id"], vm=derived)
            if st_get == 200 and got["state"] in ("done", "error"):
                break
            time.sleep(0.2)
        self.assertEqual(got["state"], "done", got)
        self.assertEqual(got["answer"], "pong")
        # The exec really ran inside the instance NAME, never a hash path.
        self.assertTrue(self.lxd.exec_requests(VM), self.lxd.requests)


class StoreTest(_Base):
    def test_orphans_are_failed_not_left_looking_slow(self):
        with db.session(self.db_path) as conn:
            task = agent_tasks.insert_task(
                conn, project_id=PROJECT_ID, vm_id=VM, node_id=self.node_id,
                command_id=None, actor="human", prompt="x", model_preference=None,
                max_tokens=16, timeout_s=300)
            conn.commit()
        with db.session(self.db_path) as conn:
            n = agent_tasks.sweep_orphans(conn, PROJECT_ID)
            self.assertEqual(n, 1)
            swept = agent_tasks.get_task(conn, task["task_id"], PROJECT_ID)
        self.assertEqual(swept["state"], "error")
        self.assertEqual(swept["error_code"], "TASK_ORPHANED")


class ExecTransportTest(unittest.TestCase):
    """`LxdExecutor.exec_instance` against the stub's exec surface."""

    def setUp(self):
        self.lxd = StubLxd()
        self.lxd.start()
        self.addCleanup(self.lxd.stop)
        self.lxd.instances["c1"] = {"name": "c1"}
        sys.path.insert(0, str(CONTROLLER))
        from app.operator.executor import LxdExecutor

        self.exe = LxdExecutor(self.lxd.base_url, timeout_s=5.0)

    def test_records_stdout(self):
        self.lxd.exec_handler = lambda argv: (0, "hello from inside\n", "")
        r = self.exe.exec_instance("c1", ["echo", "hi"], timeout_s=5.0)
        self.assertEqual(r.result, "ok", r.detail)
        self.assertEqual(r.detail, "hello from inside\n")
        self.assertEqual(self.lxd.exec_requests("c1")[0][2]["record-output"], True)
        self.assertEqual(self.lxd.exec_requests("c1")[0][2]["wait-for-websocket"], False)

    def test_json_stdout_is_not_mistaken_for_an_envelope(self):
        """The in-VM client answers JSON, so the recorded stdout is JSON too.

        `/1.0/instances/{name}/logs/<file>` is a FILE DOWNLOAD: the body is the
        output verbatim, not a `{"metadata": ...}` object. Parsing it as an object
        returned `metadata=None` for every real exec — the live defect where the
        task API answered 503 "no task surface" while the task had actually run.
        """
        payload = '{"status": 202, "body": "{}"}'
        self.lxd.exec_handler = lambda argv: (0, payload, "")
        r = self.exe.exec_instance("c1", ["python3"], timeout_s=5.0)
        self.assertEqual(r.result, "ok", r.detail)
        self.assertEqual(r.detail, payload)

    def test_proxy_envelope_around_the_log_file_is_unwrapped(self):
        """A proxy in front of LXD may wrap the file; that shape still works."""
        self.lxd.log_envelope = True
        self.lxd.exec_handler = lambda argv: (0, "wrapped\n", "")
        r = self.exe.exec_instance("c1", ["echo"], timeout_s=5.0)
        self.assertEqual(r.result, "ok", r.detail)
        self.assertEqual(r.detail, "wrapped\n")

    def test_nonzero_exit_is_an_error_with_stderr(self):
        self.lxd.exec_handler = lambda argv: (3, "", "boom\n")
        r = self.exe.exec_instance("c1", ["false"], timeout_s=5.0)
        self.assertEqual(r.result, "error")
        self.assertIn("exited 3", r.detail)
        self.assertIn("boom", r.detail)

    def test_exec_failure_is_an_error(self):
        self.lxd.exec_fail = True
        r = self.exe.exec_instance("c1", ["echo"], timeout_s=5.0)
        self.assertEqual(r.result, "error")

    def test_dry_run_sends_nothing(self):
        from app.operator.executor import LxdExecutor

        exe = LxdExecutor(self.lxd.base_url, dry_run=True)
        r = exe.exec_instance("c1", ["echo"])
        self.assertEqual(r.result, "dry_run")
        self.assertEqual(self.lxd.exec_requests(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
