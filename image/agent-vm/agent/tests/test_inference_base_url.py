"""The agent's inference base URL must always reach `/v1/chat/completions`.

Regression cover for the defect that made every agent LLM call 404 on the front
door: the shipped `agent.yaml` carried `http://10.10.0.13:8080` (no `/v1`), the
`openai` SDK appended `/chat/completions`, and the proxy serves only `/health`,
`/v1/models` and `/v1/chat/completions` (agent-runtime.md §8.2).

Both spellings are exercised with a stub transport, so the assertion is on the
*outgoing request path*, not on a string we happen to build.
"""

from __future__ import annotations

import unittest

import httpx

from termini_agent.config import (
    AgentConfig,
    BrokerConfig,
    InferenceConfig,
    LoggingConfig,
    normalize_inference_base_url,
    validate_inference_url,
)

CHAT_BODY = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 1792307000,
    "model": "gemma",
    "choices": [
        {"index": 0,
         "message": {"role": "assistant", "content": "alpha"},
         "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
}

# The two config forms an operator may write. Both must produce /v1/chat/completions.
BASE_URL_FORMS = (
    "http://10.10.0.13:8080",
    "http://10.10.0.13:8080/",
    "http://10.10.0.13:8080/v1",
    "http://10.10.0.13:8080/v1/",
)


def make_cfg(base_url: str, *, telemetry_class: str = "C0") -> AgentConfig:
    return AgentConfig(
        project_id="01J00000000000000000000000",
        vm_id="01J00000000000000000000001",
        node_id="01J00000000000000000000002",
        agent_image_version="agent-vm-2026.10.0",
        inference=InferenceConfig(
            base_url=base_url,
            default_model="gemma",
            allowed_models=("gemma",),
            token_file="/nonexistent/inference.token",  # falls back to termini-local
        ),
        broker=BrokerConfig(endpoint="http://10.20.1.1:7071"),
        logging=LoggingConfig(sink="/tmp/agent-test.jsonl"),
        raw={"governance": {"telemetry_class": telemetry_class}},
    )


def recording_transport(paths: list[str], body: dict | None = None) -> httpx.MockTransport:
    """A stub endpoint that records every request path it is asked for."""

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=body or CHAT_BODY)

    return httpx.MockTransport(handler)


class TestNormalize(unittest.TestCase):
    def test_both_forms_normalise_to_v1(self) -> None:
        for form in BASE_URL_FORMS:
            with self.subTest(base_url=form):
                self.assertEqual(normalize_inference_base_url(form),
                                 "http://10.10.0.13:8080/v1")

    def test_configured_value_is_kept_verbatim(self) -> None:
        # The status surface reports what the operator wrote; only the wire form
        # is normalised.
        cfg = make_cfg("http://10.10.0.13:8080")
        self.assertEqual(cfg.inference.base_url, "http://10.10.0.13:8080")
        self.assertEqual(cfg.inference.api_base_url, "http://10.10.0.13:8080/v1")
        self.assertEqual(cfg.inference.chat_completions_url(),
                         "http://10.10.0.13:8080/v1/chat/completions")

    def test_v1_suffix_still_passes_the_allow_list(self) -> None:
        # Host/CIDR/port checks must not break on a path component.
        for form in BASE_URL_FORMS:
            with self.subTest(base_url=form):
                validate_inference_url(form)

    def test_v1_suffix_on_a_public_host_is_still_refused(self) -> None:
        from termini_agent.config import ConfigError

        with self.assertRaises(ConfigError):
            validate_inference_url("http://8.8.8.8:8080/v1")

    def test_v1_suffix_with_a_foreign_port_is_still_refused(self) -> None:
        from termini_agent.config import ConfigError

        with self.assertRaises(ConfigError):
            validate_inference_url("http://10.10.0.13:9090/v1")


class TestClientRequestPath(unittest.TestCase):
    """Drive `InferenceClient` with a stub transport and look at the wire path."""

    def _client(self, base_url: str, paths: list[str], **kwargs):
        from termini_agent.inference import InferenceClient

        cfg = make_cfg(base_url)
        return InferenceClient(
            cfg,
            http_client=httpx.Client(transport=recording_transport(paths)),
            **kwargs,
        )

    def test_complete_targets_v1_chat_completions_for_both_forms(self) -> None:
        for form in BASE_URL_FORMS:
            with self.subTest(base_url=form):
                paths: list[str] = []
                client = self._client(form, paths)
                answer = client.complete(
                    [{"role": "user", "content": "first greek letter?"}], stream=False)
                self.assertEqual(answer, "alpha")
                self.assertEqual(paths, ["/v1/chat/completions"])
                self.assertNotIn("/chat/completions", paths)

    def test_provenance_call_targets_v1_chat_completions_for_both_forms(self) -> None:
        body = dict(CHAT_BODY)
        body["termini_backend"] = {"backend": "gemini", "answered_by": "node-c",
                                   "egress": "managed", "forwarded": True}
        for form in BASE_URL_FORMS:
            with self.subTest(base_url=form):
                paths: list[str] = []
                cfg = make_cfg(form)
                from termini_agent.inference import InferenceClient

                client = InferenceClient(
                    cfg, transport=recording_transport(paths, body))
                completion = client.complete_with_provenance(
                    [{"role": "user", "content": "hi"}])
                self.assertEqual(completion.content, "alpha")
                self.assertEqual(completion.provenance["backend"], "gemini")
                self.assertEqual(paths, ["/v1/chat/completions"])

    def test_health_probes_the_root_not_under_v1(self) -> None:
        paths: list[str] = []
        cfg = make_cfg("http://10.10.0.13:8080")
        from termini_agent.inference import InferenceClient

        client = InferenceClient(
            cfg,
            http_client=httpx.Client(transport=recording_transport(paths, {"status": "ok"})),
        )
        result = client.health()
        self.assertTrue(result["reachable"])
        self.assertEqual(paths, ["/health"])


class TestStartupLog(unittest.TestCase):
    """The effective path is logged once at startup, C0 only (§4.4)."""

    def test_c0_logs_the_effective_request_path(self) -> None:
        from termini_agent.inference import InferenceClient

        cfg = make_cfg("http://10.10.0.13:8080", telemetry_class="C0")
        with self.assertLogs("termini_agent.inference", level="INFO") as captured:
            InferenceClient(cfg, http_client=httpx.Client(
                transport=recording_transport([])))
        records = [r for r in captured.records if r.getMessage() == "inference endpoint resolved"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].termini["request_path"], "/v1/chat/completions")
        self.assertEqual(records[0].termini["api_base_url"], "http://10.10.0.13:8080/v1")

    def test_non_c0_does_not_log_the_endpoint(self) -> None:
        from termini_agent.inference import InferenceClient

        cfg = make_cfg("http://10.10.0.13:8080", telemetry_class="C1")
        with self.assertNoLogs("termini_agent.inference", level="INFO"):
            InferenceClient(cfg, http_client=httpx.Client(
                transport=recording_transport([])))


class TestAdkAdapterBaseUrl(unittest.TestCase):
    """The ADK path uses the same normalisation as `InferenceClient`."""

    def test_adk_client_base_url_ends_in_v1(self) -> None:
        try:
            from termini_agent.adk_llm import LlamaCppLlm
        except Exception as exc:  # noqa: BLE001 - ADK is only baked into the image
            self.skipTest(f"google-adk not importable here: {exc}")

        for form in BASE_URL_FORMS:
            with self.subTest(base_url=form):
                llm = LlamaCppLlm(model="gemma", inference=make_cfg(form).inference)
                self.assertEqual(str(llm._client().base_url).rstrip("/"),
                                 "http://10.10.0.13:8080/v1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
