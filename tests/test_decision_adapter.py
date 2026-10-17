"""Unit tests for the inference-side operator decision adapter.

Covers the contract's §6.2 parse + light-repair path (``termini.decision``):
well-shaped output is accepted and repaired; malformed output is detected and
returned as a typed ``DecisionError`` (never passed through, never guessed); and
transport failures/timeouts become typed errors rather than exceptions.

The model client is always a stub — no network. The controller-side guardrail
(G1–G8) is deliberately *not* exercised here: enforcing it is the controller's
job (``controller/app/operator/decision.py``), and a couple of tests below pin
down that boundary explicitly so it cannot drift silently.
"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error

from termini.decision import prompt as prompt_module
from termini.decision.adapter import decide, parse, render
from termini.decision.types import (
    ACTION_IDS,
    ACTION_SPACE,
    FALLBACK_REASONS,
    RATIONALE_MAX_CHARS,
    REASON_INFERENCE_TIMEOUT,
    REASON_INFERENCE_UNAVAILABLE,
    REASON_PARSE_ERROR,
    REASON_SCHEMA_VIOLATION,
    DecisionError,
    DecisionInput,
    DecisionResponse,
)

GOOD_COMPLETION = json.dumps(
    {
        "schema_version": 1,
        "request_id": "01M4349RQ09HMAX6RC9YPYQH4W",
        "action_id": "instance.restart",
        "params": {"instance_name": "agent-01H4KZ9Q", "force": True, "timeout_s": 30},
        "confidence": 0.87,
        "rationale": "host RAM at 95% with one instance holding 29% of RAM; restart it to reclaim.",
    }
)


def sample_input() -> DecisionInput:
    return DecisionInput(
        request_id="01M4349RQ09HMAX6RC9YPYQH4W",
        project_id="01M4349RPSZFTB60AVJEG0ZZY9",
        node_id="01M4349RPTT29RNTC6VBCXQ0RC",
        rule_id="oom-guard-01",
        type="oom",
        trigger={"metric": "ram_used_bytes", "value": 94.7, "episode": 3},
        state={"seq": 1050, "ram_used_bytes": 32641751449},
        evidence=[{"seq": 1049, "ram_used_bytes": 32300000000}],
        target_resource={
            "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
            "instances": [{"vm_id": "01M4349RPXWDC52ZDAVMPCMJSH", "instance_name": "agent-01H4KZ9Q"}],
        },
        allowed_action_space=[
            {"action_id": "instance.restart", "params_schema": {"instance_name": "string"}},
            {"action_id": "instance.stop", "params_schema": {"instance_name": "string"}},
        ],
    )


def stub_client(content: str):
    """A model client returning a fixed completion; records the call."""

    calls = []

    def _call(messages, request_id=None):
        calls.append({"messages": messages, "request_id": request_id})
        return content

    _call.calls = calls
    return _call


# ---------------------------------------------------------------------------
# Action space / enums are the contract's
# ---------------------------------------------------------------------------

class TestActionSpace(unittest.TestCase):
    def test_action_space_matches_executor_dispatch(self):
        # LxdExecutor.execute dispatches on exactly these four kinds.
        self.assertEqual(
            set(ACTION_SPACE),
            {"instance.restart", "instance.stop", "instance.delete", "image.prune"},
        )
        self.assertEqual(set(ACTION_IDS), set(ACTION_SPACE))

    def test_instance_name_is_the_only_required_param(self):
        for action_id, spec in ACTION_SPACE.items():
            required = {n for n, p in spec["params"].items() if p.get("required")}
            if spec["params"]:
                self.assertEqual(required, {"instance_name"}, action_id)
            else:
                self.assertEqual(required, set(), action_id)

    def test_fallback_reasons_enum(self):
        self.assertEqual(
            FALLBACK_REASONS,
            (
                "inference_unavailable",
                "inference_timeout",
                "parse_error",
                "schema_violation",
                "action_not_allowed",
                "invalid_target",
                "low_confidence",
            ),
        )


# ---------------------------------------------------------------------------
# parse(): accept + light repair
# ---------------------------------------------------------------------------

class TestParseAccepted(unittest.TestCase):
    def test_valid_completion(self):
        result = parse(GOOD_COMPLETION)
        self.assertIsInstance(result, DecisionResponse)
        self.assertEqual(result.action_id, "instance.restart")
        self.assertEqual(result.params["instance_name"], "agent-01H4KZ9Q")
        self.assertEqual(result.params["force"], True)
        self.assertEqual(result.params["timeout_s"], 30)
        self.assertAlmostEqual(result.confidence, 0.87)
        self.assertEqual(result.request_id, "01M4349RQ09HMAX6RC9YPYQH4W")
        self.assertEqual(result.schema_version, 1)

    def test_markdown_fence_stripped(self):
        result = parse("```json\n" + GOOD_COMPLETION + "\n```")
        self.assertIsInstance(result, DecisionResponse)
        self.assertEqual(result.action_id, "instance.restart")

    def test_unknown_param_keys_dropped(self):
        payload = {
            "schema_version": 1,
            "action_id": "instance.stop",
            "params": {"instance_name": "agent-01H4KZ9Q", "evil": "run this", "path": "/etc"},
            "confidence": 0.6,
            "rationale": "shed load",
        }
        result = parse(json.dumps(payload))
        self.assertIsInstance(result, DecisionResponse)
        self.assertEqual(set(result.params), {"instance_name", "force"})
        self.assertNotIn("evil", result.params)
        self.assertNotIn("path", result.params)

    def test_missing_optional_params_defaulted(self):
        payload = {
            "schema_version": 1,
            "action_id": "instance.restart",
            "params": {"instance_name": "agent-01H4KZ9Q"},
            "confidence": 0.9,
            "rationale": "reclaim memory",
        }
        result = parse(json.dumps(payload))
        self.assertEqual(result.params["force"], True)
        self.assertEqual(result.params["timeout_s"], 30)

    def test_no_param_action_yields_empty_params(self):
        payload = {
            "schema_version": 1,
            "action_id": "image.prune",
            "params": {"instance_name": "should-be-dropped"},
            "confidence": 0.5,
            "rationale": "reclaim disk from stale images",
        }
        result = parse(json.dumps(payload))
        self.assertIsInstance(result, DecisionResponse)
        self.assertEqual(result.params, {})

    def test_missing_params_defaults_to_empty_object(self):
        payload = {
            "schema_version": 1,
            "action_id": "image.prune",
            "confidence": 0.4,
            "rationale": "prune",
        }
        result = parse(json.dumps(payload))
        self.assertIsInstance(result, DecisionResponse)
        self.assertEqual(result.params, {})

    def test_confidence_string_coerced_to_float(self):
        payload = {
            "schema_version": 1,
            "action_id": "image.prune",
            "confidence": "0.75",
            "rationale": "prune",
        }
        result = parse(json.dumps(payload))
        self.assertIsInstance(result, DecisionResponse)
        self.assertAlmostEqual(result.confidence, 0.75)

    def test_rationale_capped_at_max(self):
        payload = {
            "schema_version": 1,
            "action_id": "image.prune",
            "confidence": 0.5,
            "rationale": "x" * (RATIONALE_MAX_CHARS + 500),
        }
        result = parse(json.dumps(payload))
        self.assertEqual(len(result.rationale), RATIONALE_MAX_CHARS)

    def test_schema_version_absent_defaults_to_one(self):
        payload = {
            "action_id": "image.prune",
            "confidence": 0.5,
            "rationale": "prune",
        }
        result = parse(json.dumps(payload))
        self.assertIsInstance(result, DecisionResponse)
        self.assertEqual(result.schema_version, 1)

    def test_request_id_omitted_when_absent_or_wrong_type(self):
        payload = {
            "schema_version": 1,
            "request_id": 12345,
            "action_id": "image.prune",
            "confidence": 0.5,
            "rationale": "prune",
        }
        result = parse(json.dumps(payload))
        self.assertIsNone(result.request_id)
        self.assertNotIn("request_id", result.to_dict())

    def test_action_id_whitespace_trimmed(self):
        payload = {
            "schema_version": 1,
            "action_id": "  instance.restart  ",
            "params": {"instance_name": "agent-01H4KZ9Q"},
            "confidence": 0.8,
            "rationale": "restart",
        }
        result = parse(json.dumps(payload))
        self.assertEqual(result.action_id, "instance.restart")


# ---------------------------------------------------------------------------
# parse(): robust extraction — a fenced / prose-wrapped / multiline reply
# ---------------------------------------------------------------------------
#
# These are the shapes that failed on the live demo (2026-10-17): Gemini
# answered, but the reply was wrapped in a ```json fence and/or polite prose, so
# `json.loads` on the whole string raised and the deterministic playbook decided
# instead of the model. CAPTURED_REPLY is the exact regression fixture; it must
# parse into a DecisionResponse without weakening the schema checks.

CAPTURED_REPLY = (
    "Here is the remediation decision for the disk degradation on this node.\n\n"
    "```json\n"
    "{\n"
    "  \"schema_version\": 1,\n"
    "  \"request_id\": \"01M5588N1CWHZMZ4KAF5PQAM1D\",\n"
    "  \"action_id\": \"instance.stop\",\n"
    "  \"params\": {\"instance_name\": \"heaviest\", \"force\": true},\n"
    "  \"confidence\": 0.88,\n"
    "  \"rationale\": \"free_bytes fell toward the floor while write_latency_ms rose; stopping the heaviest writer sheds the I/O load before the device fails.\",\n"
    "  \"incident_title\": \"Degrading disk I/O on /dev/sda\",\n"
    "  \"explanation\": \"disk.write_latency_ms is 80 ms and free space is near the 5% floor while the mount is still writable, so the device is degrading rather than failed; shedding the heaviest writer removes the load.\",\n"
    "  \"evidence\": [\n"
    "    {\"field\": \"disk.write_latency_ms\", \"value\": 80.0, \"seq\": 2077},\n"
    "    {\"field\": \"disk.mount_ro\", \"value\": false, \"seq\": 2077}\n"
    "  ],\n"
    "  \"actions\": [\n"
    "    {\"action_kind\": \"instance.stop\", \"target\": \"heaviest\", \"note\": \"shed the heaviest writer while the device is still writable\"}\n"
    "  ]\n"
    "}\n"
    "```\n\n"
    "I hope that helps."
)


class TestParseRobustExtraction(unittest.TestCase):
    def test_captured_fenced_prose_reply_parses(self):
        result = parse(CAPTURED_REPLY)
        self.assertIsInstance(result, DecisionResponse)
        self.assertEqual(result.action_id, "instance.stop")
        self.assertEqual(result.params["instance_name"], "heaviest")
        self.assertEqual(result.params["force"], True)
        self.assertAlmostEqual(result.confidence, 0.88)
        self.assertEqual(result.request_id, "01M5588N1CWHZMZ4KAF5PQAM1D")
        self.assertEqual(result.incident_title, "Degrading disk I/O on /dev/sda")
        self.assertTrue(result.explanation)
        self.assertEqual(len(result.evidence), 2)
        self.assertEqual(len(result.actions), 1)

    def test_leading_prose_before_json_is_tolerated(self):
        result = parse("My recommendation is as follows:\n" + GOOD_COMPLETION)
        self.assertIsInstance(result, DecisionResponse)
        self.assertEqual(result.action_id, "instance.restart")

    def test_trailing_prose_after_json_is_tolerated(self):
        result = parse(GOOD_COMPLETION + "\nLet me know if you need anything else.")
        self.assertIsInstance(result, DecisionResponse)
        self.assertEqual(result.action_id, "instance.restart")

    def test_multiline_pretty_json_without_fence_is_tolerated(self):
        payload = {
            "schema_version": 1,
            "action_id": "instance.stop",
            "params": {"instance_name": "heaviest", "force": True},
            "confidence": 0.8,
            "rationale": "shed load",
        }
        result = parse(json.dumps(payload, indent=2))
        self.assertIsInstance(result, DecisionResponse)
        self.assertEqual(result.action_id, "instance.stop")

    def test_fenced_json_with_trailing_prose_is_tolerated(self):
        result = parse("```json\n" + GOOD_COMPLETION + "\n```\nDone.")
        self.assertIsInstance(result, DecisionResponse)
        self.assertEqual(result.action_id, "instance.restart")

    def test_prose_without_any_json_is_still_rejected(self):
        # The extraction must not fabricate a decision out of prose: a reply
        # with no JSON at all remains a parse_error (guardrail untouched).
        result = parse("I think you should restart it, but I am not sure which one.")
        self.assertIsInstance(result, DecisionError)
        self.assertEqual(result.reason, REASON_PARSE_ERROR)

    def test_truncated_json_is_still_rejected(self):
        result = parse("```json\n{\"action_id\": \"instance.restart\", \"confidence\": 0.8")
        self.assertIsInstance(result, DecisionError)
        self.assertEqual(result.reason, REASON_PARSE_ERROR)


# ---------------------------------------------------------------------------
# parse(): detect + report malformed output (never pass through, never guess)
# ---------------------------------------------------------------------------

class TestParseRejected(unittest.TestCase):
    def assert_error(self, raw, reason):
        result = parse(raw)
        self.assertIsInstance(result, DecisionError, f"expected error for {raw!r}")
        self.assertEqual(result.reason, reason)
        return result

    def test_not_json_is_parse_error(self):
        err = self.assert_error("the best action is to restart it", REASON_PARSE_ERROR)
        self.assertTrue(err.detail)
        self.assertIsNotNone(err.raw)

    def test_truncated_json_is_parse_error(self):
        self.assert_error('{"action_id": "instance.restart", "confidence": 0.8', REASON_PARSE_ERROR)

    def test_non_object_json_is_schema_violation(self):
        self.assert_error("[1, 2, 3]", REASON_SCHEMA_VIOLATION)
        self.assert_error('"instance.restart"', REASON_SCHEMA_VIOLATION)

    def test_wrong_schema_version(self):
        raw = json.dumps({"schema_version": 2, "action_id": "image.prune",
                          "confidence": 0.5, "rationale": "r"})
        self.assert_error(raw, REASON_SCHEMA_VIOLATION)

    def test_missing_action_id(self):
        raw = json.dumps({"schema_version": 1, "confidence": 0.5, "rationale": "r"})
        self.assert_error(raw, REASON_SCHEMA_VIOLATION)

    def test_empty_action_id(self):
        raw = json.dumps({"schema_version": 1, "action_id": "", "confidence": 0.5, "rationale": "r"})
        self.assert_error(raw, REASON_SCHEMA_VIOLATION)

    def test_non_string_action_id(self):
        raw = json.dumps({"schema_version": 1, "action_id": 7, "confidence": 0.5, "rationale": "r"})
        self.assert_error(raw, REASON_SCHEMA_VIOLATION)

    def test_non_numeric_confidence(self):
        raw = json.dumps({"schema_version": 1, "action_id": "image.prune",
                          "confidence": "high", "rationale": "r"})
        self.assert_error(raw, REASON_SCHEMA_VIOLATION)

    def test_boolean_confidence_is_not_numeric(self):
        raw = json.dumps({"schema_version": 1, "action_id": "image.prune",
                          "confidence": True, "rationale": "r"})
        self.assert_error(raw, REASON_SCHEMA_VIOLATION)

    def test_missing_confidence(self):
        raw = json.dumps({"schema_version": 1, "action_id": "image.prune", "rationale": "r"})
        self.assert_error(raw, REASON_SCHEMA_VIOLATION)

    def test_missing_or_empty_rationale(self):
        raw = json.dumps({"schema_version": 1, "action_id": "image.prune", "confidence": 0.5})
        self.assert_error(raw, REASON_SCHEMA_VIOLATION)
        raw = json.dumps({"schema_version": 1, "action_id": "image.prune",
                          "confidence": 0.5, "rationale": ""})
        self.assert_error(raw, REASON_SCHEMA_VIOLATION)

    def test_non_object_params(self):
        raw = json.dumps({"schema_version": 1, "action_id": "image.prune",
                          "params": ["instance_name"], "confidence": 0.5, "rationale": "r"})
        self.assert_error(raw, REASON_SCHEMA_VIOLATION)

    def test_non_string_content_is_parse_error(self):
        result = parse({"action_id": "instance.restart"})
        self.assertIsInstance(result, DecisionError)
        self.assertEqual(result.reason, REASON_PARSE_ERROR)

    def test_decision_error_reason_must_be_in_enum(self):
        with self.assertRaises(ValueError):
            DecisionError("not_a_real_reason")


# ---------------------------------------------------------------------------
# Boundary pins: what the adapter deliberately does NOT enforce
# ---------------------------------------------------------------------------

class TestAdapterBoundary(unittest.TestCase):
    """The guardrail (G3/G5/G6) is controller-side; the adapter must not
    silently pre-empt it, and must not pretend to be the boundary."""

    def test_out_of_whitelist_action_is_passed_through_for_g3(self):
        # Well-shaped but not in ACTION_SPACE: the adapter keeps it; the
        # controller's G3 rejects it with fallback action_not_allowed.
        raw = json.dumps({"schema_version": 1, "action_id": "rm -rf /",
                          "params": {"instance_name": "x"}, "confidence": 0.99,
                          "rationale": "trust me"})
        result = parse(raw)
        self.assertIsInstance(result, DecisionResponse)
        self.assertEqual(result.action_id, "rm -rf /")
        # ...and no unexpected param keys leak through on an unknown action.
        self.assertEqual(result.params, {})

    def test_out_of_range_numeric_param_is_left_for_g5(self):
        # G5 clamps timeout_s to 1..120; the adapter does not (contract §6.2
        # lists no numeric clamping in parse).
        raw = json.dumps({"schema_version": 1, "action_id": "instance.restart",
                          "params": {"instance_name": "agent-01H4KZ9Q", "timeout_s": 999},
                          "confidence": 0.8, "rationale": "restart"})
        result = parse(raw)
        self.assertEqual(result.params["timeout_s"], 999)

    def test_high_confidence_destructive_action_is_passed_through_for_g6(self):
        raw = json.dumps({"schema_version": 1, "action_id": "instance.delete",
                          "params": {"instance_name": "agent-01H4KZ9Q"},
                          "confidence": 1.0, "rationale": "delete it"})
        result = parse(raw)
        self.assertIsInstance(result, DecisionResponse)
        self.assertEqual(result.action_id, "instance.delete")


# ---------------------------------------------------------------------------
# decide(): render + call + parse, total over transport failures
# ---------------------------------------------------------------------------

class TestDecide(unittest.TestCase):
    def test_happy_path(self):
        client = stub_client(GOOD_COMPLETION)
        result = decide(sample_input(), client=client)
        self.assertIsInstance(result, DecisionResponse)
        self.assertEqual(result.action_id, "instance.restart")
        # The correlation id was passed to the client.
        self.assertEqual(client.calls[0]["request_id"], "01M4349RQ09HMAX6RC9YPYQH4W")

    def test_timeout_returns_typed_error(self):
        def client(messages, request_id=None):
            raise TimeoutError("read timed out")

        result = decide(sample_input(), client=client)
        self.assertIsInstance(result, DecisionError)
        self.assertEqual(result.reason, REASON_INFERENCE_TIMEOUT)

    def test_urlerror_with_timeout_reason_is_timeout(self):
        def client(messages, request_id=None):
            raise urllib.error.URLError(TimeoutError("connect timed out"))

        result = decide(sample_input(), client=client)
        self.assertEqual(result.reason, REASON_INFERENCE_TIMEOUT)

    def test_connection_error_is_unavailable(self):
        def client(messages, request_id=None):
            raise urllib.error.URLError(OSError(111, "Connection refused"))

        result = decide(sample_input(), client=client)
        self.assertEqual(result.reason, REASON_INFERENCE_UNAVAILABLE)

    def test_http_error_is_unavailable(self):
        def client(messages, request_id=None):
            err = urllib.error.HTTPError(
                "http://x/v1/chat/completions", 503, "Service Unavailable", None, io.BytesIO(b"")
            )
            try:
                raise err
            finally:
                err.close()

        result = decide(sample_input(), client=client)
        self.assertEqual(result.reason, REASON_INFERENCE_UNAVAILABLE)
        self.assertIn("503", result.detail)

    def test_unexpected_exception_is_caught(self):
        def client(messages, request_id=None):
            raise RuntimeError("boom")

        result = decide(sample_input(), client=client)
        self.assertIsInstance(result, DecisionError)
        self.assertEqual(result.reason, REASON_INFERENCE_UNAVAILABLE)

    def test_non_decision_input_type_is_schema_violation(self):
        result = decide(42, client=stub_client(GOOD_COMPLETION))
        self.assertIsInstance(result, DecisionError)
        self.assertEqual(result.reason, REASON_SCHEMA_VIOLATION)

    def test_malformed_model_output_is_reported(self):
        result = decide(sample_input(), client=stub_client("I would restart it"))
        self.assertIsInstance(result, DecisionError)
        self.assertEqual(result.reason, REASON_PARSE_ERROR)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

class TestPrompt(unittest.TestCase):
    def test_message_shape(self):
        messages = render(sample_input())
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertIn(prompt_module.TEMPLATE_VERSION, messages[0]["content"])

    def test_system_prompt_enumerates_the_action_space(self):
        for action_id in ACTION_SPACE:
            self.assertIn(f'"{action_id}"', prompt_module.SYSTEM_PROMPT)

    def test_user_message_embeds_compact_json_input(self):
        messages = render(sample_input())
        user = messages[-1]["content"]
        # The DecisionInput is embedded as compact JSON with large integers
        # thousands-grouped (t_220a468a): the model sees "32,641,751,449", not a
        # bare 10-15 digit run it would then re-emit and trip G9's phone scan.
        self.assertIn('"request_id":"01M4349RQ09HMAX6RC9YPYQH4W"', user)
        self.assertIn('"ram_used_bytes":"32,641,751,449"', user)  # state, grouped
        self.assertIn('"ram_used_bytes":"32,300,000,000"', user)  # evidence, grouped
        # The raw digit runs must never reach the model verbatim.
        self.assertNotIn("32641751449", user)
        self.assertNotIn("32300000000", user)

    def test_few_shot_examples_are_valid_response_json(self):
        for example in prompt_module.FEW_SHOT:
            parsed = json.loads(example["assistant"])
            self.assertIn(parsed["action_id"], ACTION_SPACE)


# ---------------------------------------------------------------------------
# Default transport: the HTTP request must match contract §6.1 exactly
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class TestDefaultTransport(unittest.TestCase):
    """The live endpoint is unreachable from the build host (Node C is on the
    demo network), so these tests pin the wire contract by faking urlopen."""

    def _capture(self, response_payload):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            return _FakeResponse(response_payload)

        return captured, fake_urlopen

    def _config(self):
        from termini.decision.config import DecisionModelConfig

        return DecisionModelConfig(
            base_url="http://infer.internal:8080/",
            model="deepseek-flash",
            token="tok-123",
            timeout_s=12.5,
        )

    def test_request_matches_contract_section_6_1(self):
        from unittest import mock

        from termini.decision.adapter import default_client
        from termini.decision.config import REQUEST_ID_HEADER

        captured, fake = self._capture(
            {"choices": [{"message": {"content": GOOD_COMPLETION}}]}
        )
        client = default_client(self._config())
        with mock.patch("urllib.request.urlopen", fake):
            content = client(render(sample_input()), request_id="01M4349RQ09HMAX6RC9YPYQH4W")

        self.assertEqual(content, GOOD_COMPLETION)
        self.assertEqual(captured["url"], "http://infer.internal:8080/v1/chat/completions")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["timeout"], 12.5)
        self.assertEqual(captured["body"]["model"], "deepseek-flash")
        self.assertEqual(captured["body"]["temperature"], 0.0)
        self.assertEqual(captured["body"]["max_tokens"], 512)
        self.assertEqual(captured["body"]["response_format"], {"type": "json_object"})
        self.assertFalse(captured["body"]["stream"])
        self.assertEqual(captured["body"]["messages"][0]["role"], "system")
        self.assertEqual(captured["headers"].get("content-type"), "application/json")
        self.assertEqual(captured["headers"].get("authorization"), "Bearer tok-123")
        self.assertEqual(
            captured["headers"].get(REQUEST_ID_HEADER.lower()),
            "01M4349RQ09HMAX6RC9YPYQH4W",
        )

    def test_decide_end_to_end_over_faked_transport(self):
        from unittest import mock

        captured, fake = self._capture(
            {"choices": [{"message": {"content": GOOD_COMPLETION}}]}
        )
        with mock.patch("urllib.request.urlopen", fake):
            result = decide(sample_input(), config=self._config())
        self.assertIsInstance(result, DecisionResponse)
        self.assertEqual(result.action_id, "instance.restart")

    def test_bad_envelope_is_inference_unavailable(self):
        from unittest import mock

        captured, fake = self._capture({"unexpected": "shape"})
        with mock.patch("urllib.request.urlopen", fake):
            result = decide(sample_input(), config=self._config())
        self.assertIsInstance(result, DecisionError)
        self.assertEqual(result.reason, REASON_INFERENCE_UNAVAILABLE)

    def test_timeout_is_inference_timeout(self):
        from unittest import mock

        def fake(req, timeout=None):
            raise TimeoutError("read timed out")

        with mock.patch("urllib.request.urlopen", fake):
            result = decide(sample_input(), config=self._config())
        self.assertIsInstance(result, DecisionError)
        self.assertEqual(result.reason, REASON_INFERENCE_TIMEOUT)

    def test_no_token_means_no_authorization_header(self):
        from unittest import mock

        from termini.decision.adapter import default_client
        from termini.decision.config import DecisionModelConfig

        captured, fake = self._capture({"choices": [{"message": {"content": GOOD_COMPLETION}}]})
        client = default_client(DecisionModelConfig(base_url="http://infer:8080", token=""))
        with mock.patch("urllib.request.urlopen", fake):
            client(render(sample_input()), request_id=None)
        self.assertNotIn("authorization", captured["headers"])
        self.assertNotIn("x-termini-request-id", captured["headers"])

    def test_with_timeout_overrides_only_the_timeout(self):
        from termini.decision.config import DecisionModelConfig

        base = DecisionModelConfig(base_url="http://infer:8080", timeout_s=30.0)
        tighter = base.with_timeout(5.0)
        self.assertEqual(tighter.timeout_s, 5.0)
        self.assertEqual(tighter.base_url, base.base_url)
        self.assertEqual(tighter.model, base.model)

    def test_from_env_reads_the_frozen_inference_vars(self):
        from termini.decision.config import DecisionModelConfig

        cfg = DecisionModelConfig.from_env(
            {
                "TERMINI_INFERENCE_BASE_URL": "http://10.10.0.13:8080",
                "TERMINI_INFERENCE_MODEL": "deepseek-flash",
                "TERMINI_INFERENCE_TOKEN": "abc",
                "TERMINI_INFERENCE_TIMEOUT_S": "45",
            }
        )
        self.assertEqual(cfg.chat_url, "http://10.10.0.13:8080/v1/chat/completions")
        self.assertEqual(cfg.token, "abc")
        self.assertEqual(cfg.timeout_s, 45.0)


if __name__ == "__main__":
    unittest.main()
