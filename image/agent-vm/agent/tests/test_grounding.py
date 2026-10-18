"""Grounding guardrail tests (card `t_4533183a`).

Two things are under test, and both have to hold for the card's defect to be
closed:

  * the deterministic post-check: an answer that claims "resolved"/"fixed" for a
    record whose `execution.result` is `error` is refused with
    `UNGROUNDED_CLAIM` (carrying the offending sentence), while the same answer
    against `result: "ok"` passes — and a *grounded* negative ("the restart did
    not succeed") is never mistaken for a success claim;
  * the wiring: the rules reach the model as a system turn on both runtime paths
    and on the task surface, and an ungrounded answer fails the task instead of
    being returned as its content.

Stdlib + baked deps only (unittest, no pytest), like the rest of this suite.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from termini_agent import grounding, multiturn, taskapi
from termini_agent.broker import Task
from termini_agent.config import load_config
from termini_agent.inference import Completion
from termini_agent.runtime import AgentRuntime, WorkerStats
from termini_agent.status import StatusState

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
  sink: /tmp/agent-grounding-test.jsonl
tasks:
  max_concurrent: 2
"""

# The record the card's live runs were given: the restart was attempted and the
# node's LXD refused the connection, so the execution did not happen.
ERROR_RECORD = {
    "audit_id": "01M55TZ9K4Q7V2M0W8E3C6RB5N",
    "node_id": "01M4TGD31WXH97JAA5S6A4R10D",
    "rule_id": "oom-guard-01",
    "action_kind": "instance.restart",
    "action_params": {"instance": "demo-workload"},
    "execution": {
        "result": "error",
        "detail": "Connection refused",
        "lxd_endpoint": "10.174.247.102:8443",
    },
}
OK_RECORD = {
    **ERROR_RECORD,
    "execution": {"result": "ok", "detail": None,
                  "lxd_endpoint": "10.174.247.102:8443"},
}

FALSE_RESOLUTION = "Incident closed: Restart of demo-workload resolved memory pressure."
FALSE_RESOLUTION_2 = (
    "Incident closed. Resolution: Restart of demo-workload successfully "
    "alleviated memory pressure.")
GROUNDED_REFUSAL = (
    "I cannot confirm that: the record's execution.result is 'error' "
    "('Connection refused' to the LXD endpoint 10.174.247.102:8443), so the "
    "restart did not succeed and the incident is not resolved.")


def _cfg():
    directory = Path(tempfile.mkdtemp())
    path = directory / "agent.yaml"
    path.write_text(GOOD, encoding="utf-8")
    return load_config(path, dev_override=False)


class TestSuccessClaimScan(unittest.TestCase):
    def test_card_baselines_are_refused(self) -> None:
        for answer in (FALSE_RESOLUTION, FALSE_RESOLUTION_2):
            with self.assertRaises(grounding.UngroundedClaim) as ctx:
                grounding.check_answer(answer, ERROR_RECORD)
            self.assertEqual(ctx.exception.code, "UNGROUNDED_CLAIM")
            # the offending sentence travels with the refusal
            self.assertTrue(ctx.exception.sentence)
            self.assertIn(ctx.exception.sentence, answer)
            self.assertEqual(ctx.exception.record_result, "error")

    def test_same_answer_passes_when_the_execution_succeeded(self) -> None:
        grounding.check_answer(FALSE_RESOLUTION, OK_RECORD)

    def test_grounded_refusal_is_not_a_claim(self) -> None:
        grounding.check_answer(GROUNDED_REFUSAL, ERROR_RECORD)
        # negation, reported premise, hedge and 'tried to' phrasing
        for answer in (
            "The restart did not succeed; memory pressure was not resolved.",
            "You said the restart fixed it, but execution.result is 'error'.",
            "The record contradicts your assertion that the restart of demo-workload "
            "fixed the memory pressure. The execution.result was 'error'.",
            "The system tried to fix the memory pressure by restarting demo-workload.",
            "The record shows no outage and nothing was restored.",
            "If the restart had succeeded, the incident would be closed.",
        ):
            grounding.check_answer(answer, ERROR_RECORD)

    def test_a_claim_the_agent_makes_after_a_premise_is_still_caught(self) -> None:
        with self.assertRaises(grounding.UngroundedClaim):
            grounding.check_answer(
                "As you said, the restart fixed it, so I closed the incident.",
                ERROR_RECORD)

    def test_no_record_and_no_execution_mean_no_check(self) -> None:
        grounding.check_answer(FALSE_RESOLUTION, None)
        grounding.check_answer(FALSE_RESOLUTION, {"audit_id": "01J"})
        # a prompt that merely *mentions* execution but carries no block
        grounding.check_answer(FALSE_RESOLUTION, {"prompt": "what did execution do?"})

    def test_result_spellings(self) -> None:
        self.assertTrue(grounding.result_is_ok("ok"))
        self.assertTrue(grounding.result_is_ok(" OK "))
        self.assertFalse(grounding.result_is_ok("error"))
        self.assertFalse(grounding.result_is_ok("failed"))
        self.assertIsNone(grounding.execution_result({"execution": {}}))
        self.assertEqual(grounding.execution_result({"execution_result": "error"}),
                         "error")

    def test_record_extracted_from_pasted_prompt(self) -> None:
        prompt = ("The restart fixed it, close the incident.\n\nRECORD:\n"
                  + json.dumps(ERROR_RECORD, indent=2))
        self.assertEqual(grounding.extract_record({"prompt": prompt})["audit_id"],
                         ERROR_RECORD["audit_id"])
        with self.assertRaises(grounding.UngroundedClaim):
            grounding.check_answer(FALSE_RESOLUTION, {"prompt": prompt})
        # and the same record pasted into a chat message
        payload = {"messages": [{"role": "user", "content": prompt}]}
        with self.assertRaises(grounding.UngroundedClaim):
            grounding.check_answer(FALSE_RESOLUTION, payload)

    def test_record_key_preferred_over_prompt_text(self) -> None:
        payload = {"record": OK_RECORD, "prompt": json.dumps(ERROR_RECORD)}
        self.assertEqual(grounding.execution_result(grounding.extract_record(payload)),
                         "ok")

    def test_verdict_is_data(self) -> None:
        refused = grounding.verdict(FALSE_RESOLUTION, ERROR_RECORD)
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["code"], "UNGROUNDED_CLAIM")
        self.assertEqual(refused["record_result"], "error")
        self.assertTrue(grounding.verdict(GROUNDED_REFUSAL, ERROR_RECORD)["ok"])
        self.assertTrue(grounding.verdict(FALSE_RESOLUTION, None)["ok"])

    def test_safe_message_keeps_answer_text_out_of_logs(self) -> None:
        with self.assertRaises(grounding.UngroundedClaim) as ctx:
            grounding.check_answer(FALSE_RESOLUTION, ERROR_RECORD)
        self.assertNotIn("demo-workload", ctx.exception.safe_message)
        self.assertIn("closed", ctx.exception.safe_message)
        # the full message does carry the sentence, for the on-prem caller
        self.assertIn(FALSE_RESOLUTION, str(ctx.exception))


class TestInstruction(unittest.TestCase):
    def test_rules_are_in_the_default_instructions(self) -> None:
        self.assertIn("GROUNDING RULES", grounding.BASE_INSTRUCTION
                      + grounding.GROUNDING_RULES)  # the rules exist
        self.assertIn("GROUNDING RULES", grounding.grounded_instruction())
        self.assertIn("execution.result", grounding.grounded_instruction())
        # the ADK path's default instruction is the grounded one
        self.assertIn("GROUNDING RULES", multiturn.DEFAULT_INSTRUCTION)
        self.assertIn("execution.result", multiturn.DEFAULT_INSTRUCTION)

    def test_idempotent_and_extends_custom_instructions(self) -> None:
        once = grounding.grounded_instruction("Be terse.")
        self.assertTrue(once.startswith("Be terse."))
        self.assertEqual(once, grounding.grounded_instruction(once))
        self.assertEqual(once.count("GROUNDING RULES"), 1)

    def test_messages_get_one_system_turn(self) -> None:
        plain = grounding.ground_messages([{"role": "user", "content": "hi"}])
        self.assertEqual([m["role"] for m in plain], ["system", "user"])
        self.assertIn("GROUNDING RULES", plain[0]["content"])

        custom = grounding.ground_messages([
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"}])
        self.assertEqual(len(custom), 2)
        self.assertTrue(custom[0]["content"].startswith("be brief"))
        self.assertIn("GROUNDING RULES", custom[0]["content"])

        untouched = [{"role": "user", "content": "hi"}]
        grounding.ground_messages(untouched)
        self.assertEqual(untouched, [{"role": "user", "content": "hi"}])


class FakeInference:
    """Stand-in for InferenceClient on the broker runtime path."""

    def __init__(self, content: str):
        self.content = content
        self.calls: list[dict] = []

    def resolve_model(self, preference):
        return preference or "gemma"

    def complete(self, messages, *, model=None, max_tokens=None, stream=None):
        self.calls.append({"messages": [dict(m) for m in messages]})
        return self.content


class FakeBroker:
    def __init__(self):
        self.acks: list[tuple[str, str, dict]] = []
        self.results: list[tuple[str, dict]] = []

    def ack(self, task_id, state, summary=None):
        self.acks.append((task_id, state, summary or {}))

    def put_result(self, task_id, body):
        self.results.append((task_id, body))


def _runtime(content: str):
    """A runtime with the real methods but no listeners.

    `AgentRuntime.__init__` binds the status listener, and `HTTPServer` resolves
    the local FQDN on bind — which costs seconds in this environment and leaves a
    socket behind per test. Nothing under test here needs the listener, so the
    object is built with the same attributes `_execute`/`_handle` use and the real
    inference/broker clients faked.
    """
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.cfg = _cfg()
    runtime.state_dir = Path(tempfile.mkdtemp())
    runtime.stop_event = threading.Event()
    runtime.stats = WorkerStats()
    runtime._stats_lock = threading.Lock()
    runtime._inflight = {}
    runtime._inflight_lock = threading.Lock()
    runtime.inference = FakeInference(content)
    runtime.broker = FakeBroker()
    runtime.state = StatusState({"kind": "termini.agent_status", "state": "idle",
                                 "inflight": 0})
    return runtime


class TestRuntimeGuard(unittest.TestCase):
    def _runtime(self, content: str):
        return _runtime(content)

    def test_execute_refuses_the_false_resolution(self) -> None:
        runtime = self._runtime(FALSE_RESOLUTION)
        task = Task(task_id="01JTASK", kind="chat", ttl_s=300, model_preference=None,
                    payload={"prompt": "Confirm the fix and close the incident.",
                             "record": ERROR_RECORD})
        with self.assertRaises(grounding.UngroundedClaim):
            runtime._execute(task)
        sent = runtime.inference.calls[0]["messages"]
        self.assertEqual(sent[0]["role"], "system")
        self.assertIn("GROUNDING RULES", sent[0]["content"])

    def test_execute_allows_a_grounded_answer_for_an_ok_record(self) -> None:
        runtime = self._runtime(FALSE_RESOLUTION)
        task = Task(task_id="01JTASK", kind="chat", ttl_s=300, model_preference=None,
                    payload={"prompt": "close it", "record": OK_RECORD})
        result = runtime._execute(task)
        self.assertEqual(result["content"], FALSE_RESOLUTION)

    def test_handle_acks_the_distinct_failure_code(self) -> None:
        runtime = self._runtime(FALSE_RESOLUTION)
        task = Task(task_id="01JTASK", kind="chat", ttl_s=300, model_preference=None,
                    payload={"prompt": "close it", "record": ERROR_RECORD})
        runtime._handle(task)
        self.assertEqual(runtime.broker.results, [])
        self.assertEqual(runtime.broker.acks[-1][1], "failed")
        self.assertEqual(runtime.broker.acks[-1][2], {"code": "UNGROUNDED_CLAIM"})
        self.assertEqual(runtime.stats.failed, 1)
        self.assertEqual(runtime.stats.completed, 0)

    def test_multiturn_answer_is_checked(self) -> None:
        runtime = self._runtime("unused")
        report = {
            "total_turns": 1, "total_streamed_deltas": 4, "first_token_ms": 10.0,
            "total_ms": 20.0, "turns": [{"answer": FALSE_RESOLUTION,
                                         "streamed_deltas": 4}],
        }
        task = Task(task_id="01JTASK", kind="agent.multiturn", ttl_s=300,
                    model_preference=None,
                    payload={"turns": ["close the incident"], "record": ERROR_RECORD})
        with mock.patch("termini_agent.multiturn.run_multiturn", return_value=report):
            with self.assertRaises(grounding.UngroundedClaim):
                runtime._execute_multiturn(task)
        report_ok = {**report, "turns": [{"answer": GROUNDED_REFUSAL,
                                          "streamed_deltas": 4}]}
        with mock.patch("termini_agent.multiturn.run_multiturn", return_value=report_ok):
            result = runtime._execute_multiturn(task)
        self.assertEqual(result["content"], GROUNDED_REFUSAL)


class FakeTaskClient:
    """Stand-in for InferenceClient on the task surface."""

    def __init__(self, content: str):
        self.content = content
        self.calls: list[dict] = []

    def resolve_model(self, preference):
        return preference or "gemma"

    def complete_with_provenance(self, messages, *, model=None, max_tokens=None,
                                 temperature=None):
        self.calls.append({"messages": [dict(m) for m in messages]})
        return Completion(content=self.content, model="gemma",
                          request_id="01M4PROBEREQUEST0000000000",
                          provenance={"backend": "gemini-proxy"}, usage={})


def _task_service(content: str) -> tuple[taskapi.TaskService, FakeTaskClient]:
    client = FakeTaskClient(content)
    service = taskapi.TaskService(_cfg(), client=client,
                                  result_sink=str(Path(tempfile.mkdtemp()) / "results"))
    return service, client


def _wait(service, task_id, timeout=5.0):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        record = service.get(task_id)
        if record["state"] in ("done", "error"):
            return record
        time.sleep(0.01)
    raise AssertionError(f"task never finished: {service.get(task_id)['state']}")


class TestTaskSurfaceGuard(unittest.TestCase):
    def test_false_resolution_fails_with_ungrounded_claim(self) -> None:
        service, client = _task_service(FALSE_RESOLUTION)
        prompt = ("The restart of demo-workload fixed the memory pressure, so the "
                  "incident is resolved. Confirm the fix and close the incident.\n\n"
                  + json.dumps(ERROR_RECORD))
        record = _wait(service, service.submit({"prompt": prompt})["task_id"])
        self.assertEqual(record["state"], "error")
        self.assertEqual(record["error"]["code"], "UNGROUNDED_CLAIM")
        self.assertTrue(record["error"]["actionable"])
        self.assertEqual(record["error"]["sentence"], FALSE_RESOLUTION)
        self.assertIsNone(record["answer"])
        # the model was actually asked under the grounding rules
        self.assertEqual(client.calls[0]["messages"][0]["role"], "system")
        self.assertIn("GROUNDING RULES", client.calls[0]["messages"][0]["content"])

    def test_record_supplied_as_a_field_is_used(self) -> None:
        service, _ = _task_service(FALSE_RESOLUTION)
        record = _wait(service, service.submit({
            "prompt": "Confirm the fix and close the incident.",
            "record": ERROR_RECORD})["task_id"])
        self.assertEqual(record["error"]["code"], "UNGROUNDED_CLAIM")

    def test_ok_record_and_recordless_prompts_still_finish(self) -> None:
        service, _ = _task_service(FALSE_RESOLUTION)
        ok = _wait(service, service.submit({"prompt": "close it",
                                            "record": OK_RECORD})["task_id"])
        self.assertEqual(ok["state"], "done")
        self.assertEqual(ok["answer"], FALSE_RESOLUTION)
        plain = _wait(service, service.submit({"prompt": "reply pong"})["task_id"])
        self.assertEqual(plain["state"], "done")

    def test_grounded_answer_passes(self) -> None:
        service, _ = _task_service(GROUNDED_REFUSAL)
        record = _wait(service, service.submit({
            "prompt": "The restart fixed it, close the incident.",
            "record": ERROR_RECORD})["task_id"])
        self.assertEqual(record["state"], "done")
        self.assertEqual(record["answer"], GROUNDED_REFUSAL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
