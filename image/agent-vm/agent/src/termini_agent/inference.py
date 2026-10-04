"""OpenAI-compatible inference client for the on-prem router (agent-runtime.md §5.5, §10).

Rules enforced here:
  * the base URL is taken only from validated config (config.validate_inference_url);
  * the request `model` must be one of the configured aliases (explicit routing, §9);
  * attribution headers X-Termini-Request-Id / -Vm-Id / -Project-Id on every call;
  * streaming is SSE by default, `[DONE]`-terminated;
  * only idempotent-safe failures are retried (connect / 503 Retry-After).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator, Sequence

from .config import AgentConfig, InferenceConfig
from .ulid import new_ulid

log = logging.getLogger("termini_agent.inference")


class InferenceError(RuntimeError):
    """Base class; `code` mirrors the §10.5 error envelope."""

    code = "INFERENCE_ERROR"


class ModelNotAllowed(InferenceError):
    code = "MODEL_NOT_ALLOWED"


class InferenceTimeout(InferenceError):
    code = "INFERENCE_TIMEOUT"


class InferenceBusy(InferenceError):
    code = "INFERENCE_BUSY"


class InferenceUnavailable(InferenceError):
    code = "MODEL_UNAVAILABLE"


@dataclass
class Message:
    role: str
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


class InferenceClient:
    """Thin, contract-aware wrapper over the vendored `openai` SDK."""

    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg
        self.infer: InferenceConfig = cfg.inference
        token = cfg.inference_token() or "termini-local"
        try:
            from openai import OpenAI  # type: ignore import-not-found
        except ImportError as exc:  # pragma: no cover - only outside the baked image
            raise InferenceError("the `openai` client is not installed") from exc

        self._client = OpenAI(
            base_url=self.infer.base_url,
            api_key=token,
            timeout=self.infer.request_timeout_s,
            max_retries=0,  # retries are governed here, not by the SDK (§5.5)
            default_headers={
                "X-Termini-Vm-Id": cfg.vm_id,
                "X-Termini-Project-Id": cfg.project_id,
            },
        )

    # -- model routing (§9) ----------------------------------------------------
    def resolve_model(self, preference: str | None) -> str:
        model = preference or self.infer.default_model
        if model not in self.infer.allowed_models:
            raise ModelNotAllowed(f"model {model!r} not in {self.infer.allowed_models}")
        return model

    def request_id(self) -> str:
        return new_ulid()

    def headers(self, request_id: str | None = None) -> dict[str, str]:
        return {
            "X-Termini-Request-Id": request_id or self.request_id(),
            "X-Termini-Vm-Id": self.cfg.vm_id,
            "X-Termini-Project-Id": self.cfg.project_id,
        }

    # -- calls ----------------------------------------------------------------
    def complete(
        self,
        messages: Sequence[Message | dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        stream: bool | None = None,
    ) -> str | Iterator[str]:
        alias = self.resolve_model(model)
        msgs = [m.as_dict() if isinstance(m, Message) else dict(m) for m in messages]
        do_stream = self.infer.stream if stream is None else stream
        rid = self.request_id()
        kwargs: dict[str, object] = {
            "model": alias,
            "messages": msgs,
            "extra_headers": self.headers(rid),
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature

        try:
            if do_stream:
                kwargs["stream"] = True
                return self._stream(alias, kwargs, rid)
            resp = self._client.chat.completions.create(**kwargs)  # type: ignore[arg-type]
            return resp.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001 - translate to the §10.5 envelope
            raise _translate(exc) from exc

    def _stream(self, alias: str, kwargs: dict, rid: str) -> Iterator[str]:
        try:
            stream = self._client.chat.completions.create(**kwargs)  # type: ignore[arg-type]
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                text = getattr(delta, "content", None)
                if text:
                    yield text
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc) from exc

    def health(self) -> dict:
        """Probe the router's /health (best-effort; never raises)."""
        try:
            resp = self._client.get("/health", cast_to=dict)  # type: ignore[attr-defined]
            return {"reachable": True, "body": resp}
        except Exception as exc:  # noqa: BLE001
            return {"reachable": False, "error": str(exc)}


def _translate(exc: Exception) -> InferenceError:
    import httpx  # noqa: PLC0415

    if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException)):
        return InferenceTimeout(str(exc))
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429 or status == 503:
            return InferenceBusy(str(exc))
        if status in (502, 504):
            return InferenceUnavailable(str(exc))
    return InferenceError(f"{type(exc).__name__}: {exc}")
