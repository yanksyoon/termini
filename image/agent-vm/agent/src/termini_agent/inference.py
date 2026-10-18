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
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from .config import OPENAI_API_SUFFIX, AgentConfig, InferenceConfig
from .ulid import new_ulid

log = logging.getLogger("termini_agent.inference")


class InferenceError(RuntimeError):
    """Base class; `code` mirrors the §10.5 error envelope."""

    code = "INFERENCE_ERROR"


class ModelNotAllowed(InferenceError):
    code = "MODEL_NOT_ALLOWED"


class ModelNotFound(InferenceError):
    """The endpoint does not serve the alias the config names (§10.5 404)."""

    code = "MODEL_NOT_FOUND"


class InferenceTimeout(InferenceError):
    code = "INFERENCE_TIMEOUT"


class InferenceUnreachable(InferenceError):
    """Nothing is listening / no route — distinct from a slow-but-alive call."""

    code = "INFERENCE_UNREACHABLE"


class InferenceBusy(InferenceError):
    code = "INFERENCE_BUSY"


class InferenceUnavailable(InferenceError):
    code = "MODEL_UNAVAILABLE"


class EmptyCompletion(InferenceError):
    """HTTP 200 with empty content — a failure, never an empty answer."""

    code = "EMPTY_COMPLETION"


@dataclass
class Completion:
    """A non-streaming completion plus what actually answered it.

    `provenance` is taken from the endpoint's own in-band labelling (`termini_backend`
    body block / `X-Termini-Backend` header, infra/inference/proxy.py) so a caller can
    say honestly which backend served an answer — including when that backend is a
    *forwarded* managed model rather than local weights. It is never inferred here.
    """

    content: str
    model: str
    request_id: str
    provenance: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    timings: dict[str, Any] = field(default_factory=dict)
    status: int = 200


@dataclass
class Message:
    role: str
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


class InferenceClient:
    """Thin, contract-aware wrapper over the vendored `openai` SDK."""

    #: The only chat path the front door serves (agent-runtime.md §8.2).
    REQUEST_PATH = "/v1/chat/completions"

    def __init__(self, cfg: AgentConfig, *, http_client: Any | None = None,
                 transport: Any | None = None):
        self.cfg = cfg
        self.infer: InferenceConfig = cfg.inference
        token = cfg.inference_token() or "termini-local"
        # One normalisation point for the whole runtime (`http://h:8080` and
        # `http://h:8080/v1` both become `.../v1`): the SDK appends
        # `/chat/completions` to whatever it is given, and the front door serves
        # nothing outside `/v1/...`.
        self.base_url = self.infer.api_base_url
        self.request_path = self.REQUEST_PATH
        self._transport = transport  # injectable for tests (httpx.MockTransport)
        try:
            from openai import OpenAI  # type: ignore import-not-found
        except ImportError as exc:  # pragma: no cover - only outside the baked image
            raise InferenceError("the `openai` client is not installed") from exc

        kwargs: dict[str, Any] = {}
        if http_client is not None:
            kwargs["http_client"] = http_client
        self._client = OpenAI(
            base_url=self.base_url,
            api_key=token,
            timeout=self.infer.request_timeout_s,
            max_retries=0,  # retries are governed here, not by the SDK (§5.5)
            default_headers={
                "X-Termini-Vm-Id": cfg.vm_id,
                "X-Termini-Project-Id": cfg.project_id,
            },
            **kwargs,
        )
        # C0 only (§4.4): the effective path is metadata about the endpoint, never
        # customer content. Logged once so a base_url/path mismatch is visible in
        # the journal instead of only in a 404 body at the first token.
        if self.cfg.telemetry_class == "C0":
            log.info("inference endpoint resolved", extra={"termini": {
                "base_url": self.infer.base_url,
                "api_base_url": self.base_url,
                "request_path": self.request_path,
            }})

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

    def complete_with_provenance(
        self,
        messages: Sequence[Message | dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Completion:
        """One non-streaming call that also reports *what* answered it.

        Used by the task surface (§5.6): a task result must carry the endpoint's own
        provenance label (backend / answered_by / egress) next to the answer, so a
        caller never has to assume local inference. The request obeys the same rules
        as `complete` — config-scoped base URL, an allowed alias, attribution
        headers — but the transport is plain `httpx` so response headers and the
        `termini_backend` block are readable (the vendored SDK hides them).
        """
        import httpx  # noqa: PLC0415 — vendored in the image, not needed for import

        alias = self.resolve_model(model)
        msgs = [m.as_dict() if isinstance(m, Message) else dict(m) for m in messages]
        rid = self.request_id()
        payload: dict[str, Any] = {"model": alias, "messages": msgs, "stream": False}
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        if temperature is not None:
            payload["temperature"] = float(temperature)
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.cfg.inference_token() or 'termini-local'}",
                   **self.headers(rid)}
        url = self.infer.chat_completions_url()

        timeout = httpx.Timeout(self.infer.request_timeout_s,
                                connect=self.infer.connect_timeout_s)
        client_kwargs: dict[str, Any] = {"timeout": timeout}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        try:
            with httpx.Client(**client_kwargs) as client:
                resp = client.post(url, json=payload, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError) as exc:
            raise InferenceUnreachable(
                f"inference endpoint {self.infer.base_url} is unreachable: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise InferenceTimeout(
                f"inference endpoint did not answer within "
                f"{self.infer.request_timeout_s}s: {exc}") from exc
        except httpx.HTTPError as exc:
            raise InferenceError(f"{type(exc).__name__}: {exc}") from exc

        if resp.status_code >= 400:
            raise _translate_status(resp.status_code, resp.text)

        try:
            data = resp.json()
        except ValueError as exc:
            raise InferenceError(
                f"inference endpoint returned non-JSON: {resp.text[:200]}") from exc

        choices = data.get("choices") or []
        content = ""
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            content = message.get("content") or ""
        if not content.strip():
            raise EmptyCompletion(
                "inference endpoint returned HTTP 200 with empty content "
                f"(model={data.get('model')!r}, finish_reason="
                f"{(choices[0].get('finish_reason') if choices else None)!r})")
        return Completion(
            content=content,
            model=resp.headers.get("X-Termini-Model") or str(data.get("model") or alias),
            request_id=resp.headers.get("X-Termini-Request-Id") or rid,
            provenance=_provenance(data, resp.headers),
            usage=dict(data.get("usage") or {}),
            timings=dict(data.get("timings") or {}),
            status=resp.status_code,
        )

    def health(self) -> dict:
        """Probe the router's `/health` (best-effort; never raises).

        `/health` is served at the root, *not* under `/v1` (§8.2), so the probe
        strips the API suffix from the normalised base URL rather than letting the
        SDK resolve it relative to `.../v1/` (which would ask for `/v1/health`).
        """
        try:
            root = self.base_url[: -len(OPENAI_API_SUFFIX)]
            resp = self._client.get(f"{root}/health", cast_to=dict)  # type: ignore[attr-defined]
            return {"reachable": True, "body": resp}
        except Exception as exc:  # noqa: BLE001
            return {"reachable": False, "error": str(exc)}


def _translate(exc: Exception) -> InferenceError:
    import httpx  # noqa: PLC0415

    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)) and not isinstance(
            exc, httpx.ReadTimeout):
        return InferenceUnreachable(str(exc))
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException)):
        return InferenceTimeout(str(exc))
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429 or status == 503:
            return InferenceBusy(str(exc))
        if status in (502, 504):
            return InferenceUnavailable(str(exc))
    return InferenceError(f"{type(exc).__name__}: {exc}")


def _translate_status(status: int, body: str) -> InferenceError:
    """Map an HTTP error from the endpoint onto the §10.5 code family.

    The distinction that matters for the task surface is *which* failure this is:
    an unknown alias (`MODEL_NOT_FOUND`) and an endpoint with no usable backend
    (`MODEL_UNAVAILABLE`) must not be reported as a generic error, and neither may
    be mistaken for the timeout a slow model would produce.
    """
    detail = " ".join(body.split())[:300]
    text = f"inference endpoint refused the call ({status}): {detail}"
    if status == 401:
        return InferenceError("UNAUTHENTICATED: " + text)
    if status == 404:
        return ModelNotFound(text)
    if status == 413:
        return InferenceError("CONTEXT_LENGTH_EXCEEDED: " + text)
    if status == 429:
        return InferenceBusy(text)
    if status == 503:
        return InferenceUnavailable(text)
    if status == 504:
        return InferenceTimeout(text)
    if status == 400 and "model" in detail.lower():
        # The router's `400 INVALID_ARGUMENT` for `model: auto`/an unknown alias.
        return ModelNotFound(text)
    return InferenceError(text)


def _provenance(data: dict[str, Any], headers: Any) -> dict[str, Any]:
    """What answered this call, taken from the endpoint's own in-band labels.

    `infra/inference/proxy.py` puts the truth in the body (`termini_backend`) and in
    `X-Termini-*` headers; a plain llama.cpp answers with neither, in which case the
    honest answer is "unknown backend" — never an assumed "local".
    """
    block = data.get("termini_backend")
    prov: dict[str, Any] = dict(block) if isinstance(block, dict) else {}
    header_map = {
        "backend": "X-Termini-Backend",
        "answered_by": "X-Termini-Answered-By",
        "egress": "X-Termini-Egress",
        "destination": "X-Termini-Destination",
        "forwarded": "X-Termini-Forwarded",
    }
    for key, header in header_map.items():
        if prov.get(key) is None and headers.get(header):
            prov[key] = headers.get(header)
    prov.setdefault("backend", "unknown")
    prov.setdefault("forwarded", False)
    prov.setdefault("source", "endpoint-in-band" if block else "unlabelled-endpoint")
    return prov

