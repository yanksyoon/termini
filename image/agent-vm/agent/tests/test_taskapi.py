"""Unit tests for the in-VM task surface (agent-runtime.md §5.6/§7.4).

Run at bake time by `35-agent-selftest.sh` (stdlib + baked deps only), so this
file uses `unittest` and fakes the inference client — no network, no node.

What is under test is the contract, not the plumbing:
  * `queued -> running -> done` with the answer and the endpoint's OWN provenance
    label carried on the result (so a caller can name who served it);
  * the per-instance bound is enforced with `429 TASK_BOUND_EXCEEDED` + Retry-After,
    i.e. an over-bound task is refused, never silently queued;
  * every failure is a DISTINCT, actionable code — an unreachable endpoint is not
    a timeout, an empty 200 is not an answer, and a retired alias is refused
    rather than substituted (AR-6);
  * the answer body lands in the on-prem result sink (0600) and never in a log line.
"""

from __future__ import annotations

import json
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from termini_agent import taskapi
from termini_agent.config import load_config
from termini_agent.inference import (
    Completion,
    EmptyCompletion,
    InferenceUnreachable,
    ModelNotAllowed,
    ModelNotFound,
)
from termini_agent.status import StatusServer, StatusState

GOOD = """
project_id: 01J00000000000000000000000
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
  sink: /tmp/agent-taskapi-test.jsonl
tasks:
  max_concurrent: 2
"""

PROVENANCE = {
    "backend": "gemini-proxy",
    "answered_by": "forwarded-managed-model",
    "model": "gemini-2.5-flash",
    "alias": "gemma",
    "egress": "external",
    "destination": "aiplatform.googleapis.com",
    "forwarded": True,
}


def _cfg():
    d = Path(tempfile.mkdtemp())
    p = d / "agent.yaml"
    p.write_text(GOOD, encoding="utf-8")
    return load_config(p, dev_override=False)


class FakeClient:
    """Stands in for the inference client; `behaviour` decides the outcome."""

    def __init__(self, *, allowed=("gemma",), behaviour="ok", content="pong",
                 delay_s=0.0, release: threading.Event | None = None):
        self.allowed = tuple(allowed)
        self.behaviour = behaviour
        self.content = content
        self.delay_s = delay_s
        self.release = release
        self.calls: list[dict] = []

    def resolve_model(self, preference):
        model = preference or self.allowed[0]
        if model not in self.allowed:
            raise ModelNotAllowed(f"model {model!r} not in {list(self.allowed)}")
        return model

    def complete_with_provenance(self, messages, *, model=None, max_tokens=None,
                                 temperature=None):
        self.calls.append({"model": model, "max_tokens": max_tokens,
                           "messages": [dict(m) for m in messages]})
        if self.release is not None:
            self.release.wait(5.0)
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.behaviour == "unreachable":
            raise InferenceUnreachable(
                "inference endpoint http://10.10.0.13:8080 is unreachable: "
                "All connection attempts failed")
        if self.behaviour == "timeout":
            from termini_agent.inference import InferenceTimeout

            raise InferenceTimeout("inference endpoint did not answer within 120.0s")
        if self.behaviour == "empty":
            raise EmptyCompletion("inference endpoint returned HTTP 200 with empty content")
        if self.behaviour == "notfound":
            raise ModelNotFound("inference endpoint refused the call (404): model not found")
        return Completion(content=self.content, model="gemma",
                          request_id="01M4PROBEREQUEST0000000000",
                          provenance=dict(PROVENANCE),
                          usage={"prompt_tokens": 5, "completion_tokens": 1,
                                 "total_tokens": 6},
                          timings={"wall_ms": 851.87})


def _wait_state(service: taskapi.TaskService, task_id: str, states, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = service.get(task_id)
        if rec["state"] in states:
            return rec
        time.sleep(0.01)
    raise AssertionError(f"task {task_id} never reached {states}: "
                         f"{service.get(task_id)['state']}")


class TestTaskService(unittest.TestCase):
    def _service(self, client=None, **kw):
        sink = Path(tempfile.mkdtemp()) / "results"
        return taskapi.TaskService(_cfg(), client=client or FakeClient(),
                                   result_sink=str(sink), **kw), sink

    def test_queued_to_done_with_provenance_and_sink(self) -> None:
        client = FakeClient(content="pong")
        service, sink = self._service(client)
        rec = service.submit({"prompt": "reply pong", "actor": "human:ops"})
        self.assertEqual(rec["state"], "queued")
        self.assertNotIn("answer", rec)
        done = _wait_state(service, rec["task_id"], ("done", "error"))
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["answer"], "pong")
        self.assertEqual(done["model"], "gemma")
        # The provenance is the endpoint's own label — never assumed "local".
        self.assertEqual(done["provenance"]["backend"], "gemini-proxy")
        self.assertTrue(done["provenance"]["forwarded"])
        self.assertEqual(done["provenance"]["destination"], "aiplatform.googleapis.com")
        self.assertEqual(done["tokens"], 1)
        self.assertEqual(done["actor"], "human:ops")
        self.assertIsNotNone(done["result_ref"])
        written = json.loads(Path(done["result_ref"]).read_text(encoding="utf-8"))
        self.assertEqual(written["answer"], "pong")
        mode = stat.S_IMODE(Path(done["result_ref"]).stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(client.calls[0]["model"], "gemma")

    def test_bound_is_enforced_not_queued(self) -> None:
        release = threading.Event()
        client = FakeClient(release=release)
        service, _ = self._service(client, bound=1)
        first = service.submit({"prompt": "slow"})
        with self.assertRaises(taskapi.TaskBoundExceeded) as ctx:
            service.submit({"prompt": "second"})
        self.assertEqual(ctx.exception.code, "TASK_BOUND_EXCEEDED")
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(ctx.exception.retry_after, 5)
        release.set()
        _wait_state(service, first["task_id"], ("done", "error"))
        # Once the slot frees up the instance accepts work again.
        second = service.submit({"prompt": "third"})
        _wait_state(service, second["task_id"], ("done", "error"))

    def test_unreachable_endpoint_is_distinct_and_actionable(self) -> None:
        service, _ = self._service(FakeClient(behaviour="unreachable"))
        rec = service.submit({"prompt": "x"})
        done = _wait_state(service, rec["task_id"], ("done", "error"))
        self.assertEqual(done["state"], "error")
        self.assertEqual(done["error"]["code"], "INFERENCE_UNREACHABLE")
        self.assertIn("node C", done["error"]["actionable"])
        self.assertIsNone(done["answer"])

    def test_timeout_is_not_success(self) -> None:
        service, _ = self._service(FakeClient(behaviour="timeout"))
        done = _wait_state(service, service.submit({"prompt": "x"})["task_id"],
                           ("done", "error"))
        self.assertEqual(done["state"], "error")
        self.assertEqual(done["error"]["code"], "INFERENCE_TIMEOUT")
        self.assertIsNone(done["answer"])

    def test_empty_completion_is_a_failure(self) -> None:
        service, _ = self._service(FakeClient(behaviour="empty"))
        done = _wait_state(service, service.submit({"prompt": "x"})["task_id"],
                           ("done", "error"))
        self.assertEqual(done["error"]["code"], "EMPTY_COMPLETION")

    def test_retired_alias_is_refused_not_substituted(self) -> None:
        service, _ = self._service(FakeClient(behaviour="notfound"))
        # `gemma` is allowed, so the refusal has to come from resolution.
        with self.assertRaises(taskapi.TaskError) as ctx:
            service.submit({"prompt": "x", "model_preference": "deepseek-flash"})
        self.assertEqual(ctx.exception.code, "MODEL_NOT_ALLOWED")

    def test_model_not_found_at_the_endpoint_is_surfaced(self) -> None:
        service, _ = self._service(FakeClient(behaviour="notfound"))
        done = _wait_state(service, service.submit({"prompt": "x"})["task_id"],
                           ("done", "error"))
        self.assertEqual(done["error"]["code"], "MODEL_NOT_FOUND")

    def test_prompt_bound(self) -> None:
        service, _ = self._service()
        with self.assertRaises(taskapi.TaskTooLarge):
            service.submit({"prompt": "a" * 9000})

    def test_messages_are_accepted(self) -> None:
        client = FakeClient()
        service, _ = self._service(client)
        done = _wait_state(service, service.submit({"messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"}]})["task_id"], ("done", "error"))
        self.assertEqual(done["state"], "done")
        self.assertEqual(len(client.calls[0]["messages"]), 2)

    def test_missing_prompt_is_rejected(self) -> None:
        service, _ = self._service()
        with self.assertRaises(taskapi.TaskInvalid):
            service.submit({})

    def test_counters_are_c0_only(self) -> None:
        service, _ = self._service()
        done = _wait_state(service, service.submit({"prompt": "secret"})["task_id"],
                           ("done", "error"))
        counters = service.counters()
        self.assertEqual(counters["bound"], 2)
        self.assertEqual(counters["in_flight"], 0)
        self.assertEqual(counters["completed"], 1)
        self.assertNotIn("answer", counters)
        self.assertNotIn("secret", json.dumps(counters))
        self.assertEqual(service.list()["tasks"][0]["task_id"], done["task_id"])
        # the listing is a summary: the answer is only on the single-task read
        self.assertNotIn("answer", service.list()["tasks"][0])


class TestTaskHTTP(unittest.TestCase):
    """The surface as the node's control plane drives it (localhost HTTP)."""

    def setUp(self) -> None:
        self.client = FakeClient()
        self.service = taskapi.TaskService(
            _cfg(), client=self.client,
            result_sink=str(Path(tempfile.mkdtemp()) / "results"))
        self.state = StatusState({"kind": "termini.agent_status", "state": "idle",
                                  "tasks": self.service.counters()})
        self.server = StatusServer(self.state, http_port=0, unix_path=None,
                                   tasks=self.service)
        self.server.start()
        self.base = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self) -> None:
        self.server.stop()

    def _request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, dict(resp.headers), json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), json.loads(exc.read() or b"{}")

    def test_submit_then_read_over_http(self) -> None:
        status, headers, body = self._request("POST", "/v1/tasks",
                                              {"prompt": "reply pong", "actor": "human"})
        self.assertEqual(status, 202)
        self.assertEqual(body["state"], "queued")
        self.assertEqual(headers.get("X-Termini-Task-Id"), body["task_id"])
        task_id = body["task_id"]
        deadline = time.time() + 5
        while time.time() < deadline:
            status, _, read = self._request("GET", f"/v1/tasks/{task_id}")
            if read["state"] in ("done", "error"):
                break
            time.sleep(0.02)
        self.assertEqual(read["state"], "done")
        self.assertEqual(read["answer"], "pong")

    def test_bound_over_http_is_429_with_retry_after(self) -> None:
        self.service.bound = 1
        self.client.release = threading.Event()
        status, _, first = self._request("POST", "/v1/tasks", {"prompt": "slow"})
        self.assertEqual(status, 202)
        status, headers, body = self._request("POST", "/v1/tasks", {"prompt": "second"})
        self.assertEqual(status, 429)
        self.assertEqual(body["error"]["code"], "TASK_BOUND_EXCEEDED")
        self.assertTrue(body["error"]["actionable"])
        self.assertEqual(headers.get("Retry-After"), "5")
        self.client.release.set()
        self._request("GET", f"/v1/tasks/{first['task_id']}")

    def test_oversize_prompt_over_http_is_413(self) -> None:
        status, _, body = self._request("POST", "/v1/tasks", {"prompt": "x" * 9000})
        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "PROMPT_TOO_LARGE")

    def test_unknown_task_is_404(self) -> None:
        status, _, body = self._request("GET", "/v1/tasks/01J0000000000000000000000ZZ")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "TASK_NOT_FOUND")

    def test_status_still_served_and_carries_task_counters(self) -> None:
        self.service.submit({"prompt": "x"})
        status, _, body = self._request("GET", "/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "idle")
        self.assertIn("tasks", body)

    def test_listing_over_http(self) -> None:
        self._request("POST", "/v1/tasks", {"prompt": "one"})
        status, _, body = self._request("GET", "/v1/tasks?limit=5")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["bound"], 2)
        self.assertNotIn("answer", body["tasks"][0])

    def test_get_does_not_leak_into_status(self) -> None:
        """The C0 status surface carries counters, never task content."""
        status, _, body = self._request("POST", "/v1/tasks", {"prompt": "top secret"})
        time.sleep(0.1)
        status_body = json.dumps(self._request("GET", "/status")[2])
        self.assertNotIn("top secret", status_body)
        self.assertIn("tasks", self._request("GET", "/status")[2])
        read = self._request("GET", f"/v1/tasks/{body['task_id']}")[2]
        self.assertEqual(read["state"], "done")
        self.assertEqual(read["answer"], "pong")


if __name__ == "__main__":
    unittest.main(verbosity=2)
