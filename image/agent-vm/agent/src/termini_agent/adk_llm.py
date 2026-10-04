"""Google-ADK model adapter for the on-prem llama.cpp endpoint (agent-runtime.md §5.5, §10).

The agent image ships `google-adk` (image/agent-vm/provision/20-agent-libs.sh) but
**not** `litellm`, which ADK's stock `LiteLlm` adapter requires. Rather than add a
dependency, this module implements a first-class ADK `BaseLlm` that speaks the
OpenAI Chat Completions wire protocol directly against the frozen inference base
URL (§19.1). That keeps the ADK agent on the same governance-scoped endpoint as the
rest of the runtime: the model can only ever be the configured on-prem alias.

The adapter:
  * converts ADK `LlmRequest` contents + system instruction into OpenAI messages,
  * calls the endpoint with attribution headers (§5.5) and SSE streaming,
  * records every delta into a `ChunkRecorder` so the caller can prove streaming,
  * tolerates both server reasoning configs (inline `content`, split `reasoning_content`).
"""

from __future__ import annotations

from typing import AsyncGenerator, List

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types as gtypes
from pydantic import PrivateAttr

from .config import InferenceConfig
from .stream_metrics import ChunkRecorder

# Reasoning text is the model's private scratchpad; a stock llama.cpp server emits
# it on this field while the frozen `--reasoning-format none` config inlines it in
# `content`. Either way it is a real generated token and counts as streaming.
_REASONING_FIELD = "reasoning_content"


def _delta_text(delta: object) -> tuple[str, str] | None:
    """Return (text, kind) for a streaming delta, or None when it carries no text."""
    content = getattr(delta, "content", None)
    if content:
        return content, "content"
    reasoning = getattr(delta, _REASONING_FIELD, None)
    if reasoning:
        return reasoning, "reasoning"
    return None


def _messages_from_request(llm_request: LlmRequest) -> list[dict[str, str]]:
    """Flatten an ADK LlmRequest into OpenAI chat messages (system + history)."""
    messages: list[dict[str, str]] = []
    system_instruction = getattr(llm_request.config, "system_instruction", None) if llm_request.config else None
    if system_instruction:
        if isinstance(system_instruction, str):
            messages.append({"role": "system", "content": system_instruction})
        else:  # google.genai.types.Content
            text = "".join(p.text or "" for p in (getattr(system_instruction, "parts", None) or []))
            if text:
                messages.append({"role": "system", "content": text})
    for content in llm_request.contents or []:
        role = "assistant" if content.role == "model" else (content.role or "user")
        text = "".join(p.text or "" for p in (content.parts or []))
        messages.append({"role": role, "content": text})
    return messages


class LlamaCppLlm(BaseLlm):
    """ADK `BaseLlm` backed by the host-exposed llama.cpp OpenAI-compatible API."""

    model: str  # ADK model name == the request alias (deepseek-flash / deepseek-v4-pro)

    # BaseLlm is a pydantic model; runtime wiring lives in private attributes so it
    # is not part of the (de)serialised model schema.
    _inference: InferenceConfig = PrivateAttr()
    _token: str = PrivateAttr(default="termini-local")
    _vm_id: str = PrivateAttr(default="")
    _project_id: str = PrivateAttr(default="")
    _request_id: str = PrivateAttr(default="")
    _recorder: ChunkRecorder | None = PrivateAttr(default=None)
    _max_tokens: int = PrivateAttr(default=256)
    _temperature: float = PrivateAttr(default=0.2)
    _last_usage: dict | None = PrivateAttr(default=None)

    def __init__(self, *, model: str, inference: InferenceConfig, token: str | None = None,
                 vm_id: str = "", project_id: str = "", recorder: ChunkRecorder | None = None,
                 max_tokens: int = 256, temperature: float = 0.2, request_id: str = ""):
        super().__init__(model=model)
        self._inference = inference
        self._token = token or "termini-local"
        self._vm_id = vm_id
        self._project_id = project_id
        self._request_id = request_id
        self._recorder = recorder
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._last_usage = None

    # -- ADK contract ----------------------------------------------------------
    @classmethod
    def supported_models(cls) -> List[str]:
        # Any alias is accepted; the endpoint itself rejects unknown models (§9).
        return [r".*"]

    def _headers(self) -> dict[str, str]:
        headers = {}
        if self._request_id:
            headers["X-Termini-Request-Id"] = self._request_id
        if self._vm_id:
            headers["X-Termini-Vm-Id"] = self._vm_id
        if self._project_id:
            headers["X-Termini-Project-Id"] = self._project_id
        return headers

    def _client(self):
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            base_url=self._inference.base_url,
            api_key=self._token,
            timeout=self._inference.request_timeout_s,
            max_retries=0,
        )

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        messages = _messages_from_request(llm_request)
        client = self._client()
        # Always ask the server for SSE: it is the only way to observe per-token
        # arrival, and it lets us honour a non-streaming ADK call with one response.
        response = await client.chat.completions.create(
            model=self.model,
            messages=messages,  # type: ignore[arg-type]
            stream=True,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            extra_headers=self._headers() or None,
            stream_options={"include_usage": True},
        )
        collected: list[str] = []
        async for chunk in response:
            if getattr(chunk, "usage", None):
                self._last_usage = {
                    "prompt_tokens": getattr(chunk.usage, "prompt_tokens", None),
                    "completion_tokens": getattr(chunk.usage, "completion_tokens", None),
                    "total_tokens": getattr(chunk.usage, "total_tokens", None),
                }
            if not getattr(chunk, "choices", None):
                continue
            parsed = _delta_text(chunk.choices[0].delta)
            if parsed is None:
                continue
            text, kind = parsed
            collected.append(text)
            if self._recorder is not None:
                self._recorder.record(text, kind)
            if stream:
                yield LlmResponse(
                    content=gtypes.Content(role="model", parts=[gtypes.Part(text=text)]),
                    partial=True,
                )
        full = "".join(collected)
        yield LlmResponse(
            content=gtypes.Content(role="model", parts=[gtypes.Part(text=full)]),
            turn_complete=True,
        )
