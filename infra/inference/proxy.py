#!/usr/bin/env python3
"""termini-inference-proxy — Node C's inference front door (the simulated GPU node).

WHAT THIS IS
------------
Nodes A/B call `http://10.10.0.13:8080` with the model alias `gemma` and nothing
changes for them. What answers on that address is no longer `llama-server`
directly but this proxy, which is a **drop-in** for the endpoints the frozen
contracts define (`docs/contracts/agent-runtime.md` AR-4/§8/§10):

    GET  /health               -> 200 {"status":"ok", ...}
    GET  /v1/models            -> {"object":"list","data":[{"id":"gemma", ...}]}
    POST /v1/chat/completions  -> OpenAI chat-completions shape, streaming (SSE)
                                  and non-streaming, with `usage` and `timings`

The backend is chosen by CONFIGURATION, not code (`TERMINI_INFERENCE_BACKEND`):

  * `local`        — transparent passthrough to the local llama.cpp worker
                     (`TERMINI_INFERENCE_UPSTREAM`, loopback by default). The
                     bytes that come back are llama.cpp's own bytes: same
                     weights, same CPU, same envelope. This is the path that
                     must keep working, and it does.
  * `gemini-proxy` — the request is TRANSLATED to Vertex AI `generateContent`
                     and the answer is returned in the SAME envelope. This is a
                     forwarded managed model emulating a GPU node. It is NOT
                     GPU inference, NOT on-prem inference and NOT Gemma, and
                     every response, every log line and the operator's audit row
                     say so in-band:

                       headers: X-Termini-Backend: gemini-proxy
                                X-Termini-Model-Id: gemini-2.5-flash
                                X-Termini-External-Destination: aiplatform.googleapis.com
                                X-Termini-Egress: external
                                X-Termini-Answered-By: forwarded-managed-model
                       body:    "termini_backend": {
                                  "backend": "gemini-proxy",
                                  "model": "gemini-2.5-flash",
                                  "destination": "aiplatform.googleapis.com",
                                  "answered_by": "forwarded-managed-model",
                                  "egress": "external" }

**WHICH OF THE TWO SURFACES THIS PROCESS IS (D9)**

The same file runs both halves of the inference split
(`docs/contracts/infra-decisions.md` **D9**); the difference is configuration:

* the **agent-facing** surface is Node C's front door on `0.0.0.0:8080` with
  `TERMINI_INFERENCE_BACKEND=local` — client/agent traffic is answered by that
  node's own weights and the port offers nothing else;
* the **provider-only** surface is the CONTROLLER's listener on `127.0.0.1:8085`
  with `TERMINI_INFERENCE_BACKEND=gemini-proxy` — the operator's diagnosis call
  leaves for `aiplatform.googleapis.com`, the listener is bound to loopback (the
  fleet has no route to it), and it is where a diagnosis is allowed to originate.

Flipping one does not touch the other: separate env file, separate unit, separate
address. Neither backend stops existing — the split is about which one is offered
WHERE.

FAILURE SEMANTICS
-----------------
The forwarded path fails LOUDLY and FAST, and never hangs the caller: a distinct
error code, a distinct log event (`upstream_failure`), a `Retry-After` where the
upstream gave one, and always inside the request timeout. The controller's
decision path turns any non-2xx into `inference_unavailable` and falls back to
the deterministic fast-path — a request is never silently answered by nobody.

AUTH
----
The HOST's OWN identity: an OAuth access token minted per call from the GCE
metadata server (`aiplatform.googleapis.com` needs `cloud-platform`). On Node C
that is the node's least-privilege identity (task `t_5a4a0f73`); on the controller
it is the controller's dedicated provider service account (D9). **No
service-account key file exists on either host and none may be added** — the
metadata identity is the only credential. `TERMINI_GEMINI_ACCESS_TOKEN` exists
only so tests can inject one.

Stdlib only: the node image has Python but the proxy must not add a dependency to
the node (no pip at request time, no venv to keep in sync).
"""

from __future__ import annotations

import argparse
import collections
import http.client
import json
import os
import random
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

BACKEND_LOCAL = "local"
BACKEND_GEMINI = "gemini-proxy"
BACKENDS = (BACKEND_LOCAL, BACKEND_GEMINI)

ANSWERED_BY_LOCAL = "local-weights"
ANSWERED_BY_FORWARDED = "forwarded-managed-model"
EGRESS_IN_VPC = "in-vpc"
EGRESS_EXTERNAL = "external"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
DEFAULT_ALIAS = "gemma"
DEFAULT_UPSTREAM = "http://127.0.0.1:8081"
DEFAULT_GEMINI_HOST = "aiplatform.googleapis.com"
DEFAULT_GEMINI_LOCATION = "global"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TEMPERATURE = 0.2
DEFAULT_STATE_FILE = "/run/termini/inference-backend.json"

# Contract §10.5 error codes.
E_INVALID = "INVALID_ARGUMENT"
E_AUTH = "UNAUTHENTICATED"
E_MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
E_CONTEXT = "CONTEXT_LENGTH_EXCEEDED"
E_BUSY = "INFERENCE_BUSY"
E_UNAVAILABLE = "MODEL_UNAVAILABLE"
E_TIMEOUT = "INFERENCE_TIMEOUT"
E_ERROR = "INFERENCE_ERROR"

_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)
_METADATA_PROJECT_URL = "http://metadata.google.internal/computeMetadata/v1/project/project-id"

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id(prefix: str = "chatcmpl-") -> str:
    """A ULID-shaped correlation id (no dependency on the controller's module)."""
    ms = int(time.time() * 1000)
    ts = ""
    for _ in range(10):
        ts = _ALPHABET[ms & 31] + ts
        ms >>= 5
    rand = "".join(random.choice(_ALPHABET) for _ in range(16))
    return prefix + ts + rand


# --------------------------------------------------------------------------- #
# Configuration (env, i.e. configuration decides the backend — not code)
# --------------------------------------------------------------------------- #

@dataclass
class Config:
    backend: str = BACKEND_LOCAL
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    alias: str = DEFAULT_ALIAS
    upstream: str = DEFAULT_UPSTREAM
    upstream_timeout_s: float = 900.0
    gemini_host: str = DEFAULT_GEMINI_HOST
    gemini_location: str = DEFAULT_GEMINI_LOCATION
    gemini_model: str = DEFAULT_GEMINI_MODEL
    gemini_project: str = ""
    gemini_timeout_s: float = DEFAULT_TIMEOUT_S
    gemini_thinking_budget: int = 0
    access_token: str = ""
    state_file: str = DEFAULT_STATE_FILE
    log_file: str = ""
    server_header: str = "termini-inference-proxy"

    @property
    def supported_alias(self) -> str:
        return self.alias

    @property
    def answered_by(self) -> str:
        return ANSWERED_BY_FORWARDED if self.backend == BACKEND_GEMINI else ANSWERED_BY_LOCAL

    @property
    def egress(self) -> str:
        return EGRESS_EXTERNAL if self.backend == BACKEND_GEMINI else EGRESS_IN_VPC

    @property
    def served_model(self) -> str:
        """The model id actually producing tokens (never a local alias when forwarded)."""
        return self.gemini_model if self.backend == BACKEND_GEMINI else self.alias

    @classmethod
    def from_env(cls, environ: dict | None = None) -> "Config":
        env = os.environ if environ is None else environ

        def s(name: str, default: str) -> str:
            value = (env.get(name) or "").strip()
            return value or default

        def f(name: str, default: float) -> float:
            try:
                return float(s(name, str(default)))
            except ValueError:
                return default

        def i(name: str, default: int) -> int:
            try:
                return int(float(s(name, str(default))))
            except ValueError:
                return default

        backend = s("TERMINI_INFERENCE_BACKEND", BACKEND_LOCAL).lower()
        if backend not in BACKENDS:
            raise ValueError(
                f"TERMINI_INFERENCE_BACKEND={backend!r} is not one of {BACKENDS}"
            )
        return cls(
            backend=backend,
            host=s("TERMINI_INFERENCE_HOST", DEFAULT_HOST),
            port=i("TERMINI_INFERENCE_PORT", DEFAULT_PORT),
            alias=s("TERMINI_INFERENCE_ALIAS", DEFAULT_ALIAS),
            upstream=s("TERMINI_INFERENCE_UPSTREAM", DEFAULT_UPSTREAM).rstrip("/"),
            upstream_timeout_s=f("TERMINI_INFERENCE_UPSTREAM_TIMEOUT_S", 900.0),
            gemini_host=s("TERMINI_GEMINI_HOST", DEFAULT_GEMINI_HOST).rstrip("/"),
            gemini_location=s("TERMINI_GEMINI_LOCATION", DEFAULT_GEMINI_LOCATION),
            gemini_model=s("TERMINI_GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
            gemini_project=s("TERMINI_GEMINI_PROJECT", ""),
            gemini_timeout_s=f("TERMINI_GEMINI_TIMEOUT_S", DEFAULT_TIMEOUT_S),
            gemini_thinking_budget=i("TERMINI_GEMINI_THINKING_BUDGET", 0),
            access_token=s("TERMINI_GEMINI_ACCESS_TOKEN", ""),
            state_file=s("TERMINI_INFERENCE_STATE_FILE", DEFAULT_STATE_FILE),
            log_file=s("TERMINI_INFERENCE_LOG_FILE", ""),
        )

    # -- identity ----------------------------------------------------------- #
    def gemini_base(self) -> str:
        host = self.gemini_host
        if not host.startswith(("http://", "https://")):
            host = "https://" + host
        return host.rstrip("/")

    def generate_url(self, stream: bool) -> str:
        verb = "streamGenerateContent?alt=sse" if stream else "generateContent"
        return (
            f"{self.gemini_base()}/v1/projects/{self.gemini_project}"
            f"/locations/{self.gemini_location}/publishers/google/models/"
            f"{self.gemini_model}:{verb}"
        )

    def external_destination(self) -> str:
        """The host a forwarded call actually leaves for (the boundary crossing)."""
        return urllib.parse.urlsplit(self.gemini_base()).hostname or self.gemini_host

    def upstream_url(self, path: str) -> str:
        """The request TARGET for the local worker (origin-form, as llama.cpp expects)."""
        return "/" + path.lstrip("/")

    def upstream_host_port(self) -> tuple[str, int]:
        parts = urllib.parse.urlsplit(self.upstream)
        return parts.hostname or "127.0.0.1", parts.port or 80


# --------------------------------------------------------------------------- #
# Logging — every line carries the backend label
# --------------------------------------------------------------------------- #

class RequestLog:
    """One JSON object per request, to stderr (journald) and an optional file.

    A forwarded call NEVER logs "GPU" or "local" as its answer: `answered_by`
    is `forwarded-managed-model` and `backend` is `gemini-proxy`.

    `stream` is the sink the JSON line is written to. When it is None the line
    goes to `sys.stderr`, resolved per write, which is what journald wants. A
    caller may inject its own stream instead: the handler runs on its own
    thread and writes its request line after the response body, so a reader
    that swaps the process-global `sys.stderr` and then reads it back is
    measuring a race, not the log. Every emitted record is also appended to
    `self.records` (the most recent 100) so the log can be asserted on without
    scraping a stream at all.
    """

    def __init__(self, cfg: Config, stream=None) -> None:
        self.cfg = cfg
        self.stream = stream
        self.records: collections.deque = collections.deque(maxlen=100)
        self._lock = threading.Lock()
        self._fh = None
        if cfg.log_file:
            try:
                self._fh = open(cfg.log_file, "a", encoding="utf-8")  # noqa: SIM115
            except OSError as exc:  # pragma: no cover - only when the path is unwritable
                print(f"termini-inference-proxy: cannot open log {cfg.log_file}: {exc}",
                      file=sys.stderr, flush=True)

    def emit(self, event: str, severity: str = "info", **fields) -> dict:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            "event": event,
            "severity": severity,
            "service": "termini-inference-proxy",
            "backend": self.cfg.backend,
            "model": self.cfg.served_model,
            "alias": self.cfg.alias,
            "answered_by": self.cfg.answered_by,
            "egress": self.cfg.egress,
            "destination": (
                self.cfg.external_destination()
                if self.cfg.backend == BACKEND_GEMINI
                else self.cfg.upstream
            ),
        }
        record.update({k: v for k, v in fields.items() if v is not None})
        line = json.dumps(record, separators=(",", ":"), default=str)
        sink = self.stream if self.stream is not None else sys.stderr
        with self._lock:
            # Record first, then write: a reader that waits for the record can
            # then rely on the line having been written to the sink too.
            self.records.append(record)
            print(line, file=sink, flush=True)
            if self._fh is not None:
                self._fh.write(line + "\n")
                self._fh.flush()
        return record

    def request(self, **fields) -> dict:
        return self.emit("inference_request", **fields)

    def failure(self, distinct: str, **fields) -> dict:
        """The loud, distinct failure line (requirement: fail loudly, not silently)."""
        return self.emit("upstream_failure", severity="error", distinct=distinct, **fields)


# --------------------------------------------------------------------------- #
# Errors — the frozen §10.5 envelope, always labelled
# --------------------------------------------------------------------------- #

class ProxyError(Exception):
    def __init__(self, status: int, code: str, message: str, *, param: str = "",
                 detail: str = "", retry_after: int | None = None,
                 distinct: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.param = param
        self.detail = detail
        self.retry_after = retry_after
        # A distinct machine-readable reason for the LOUD failure path; empty for
        # client-side argument errors (those are not an upstream outage).
        self.distinct = distinct


def error_body(cfg: Config, request_id: str, err: ProxyError) -> dict:
    """The §10.5 envelope plus the in-band backend/destination labelling.

    `detail` never carries prompt or customer content — only upstream status text.
    """
    body = {
        "error": {
            "message": err.message,
            "type": "termini_error",
            "code": err.code,
            "param": err.param or None,
            "request_id": request_id,
        }
    }
    if err.detail:
        body["error"]["detail"] = err.detail
    if err.distinct:
        body["error"]["distinct"] = err.distinct
    body["error"]["backend"] = cfg.backend
    body["error"]["model"] = cfg.served_model
    if cfg.backend == BACKEND_GEMINI:
        body["error"]["destination"] = cfg.external_destination()
        body["error"]["egress"] = EGRESS_EXTERNAL
    return body


# --------------------------------------------------------------------------- #
# Auth — the node's own metadata-server identity (no key file)
# --------------------------------------------------------------------------- #

def metadata_token(timeout: float = 10.0) -> str:
    req = urllib.request.Request(_METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))["access_token"]


def metadata_project(timeout: float = 5.0) -> str:
    req = urllib.request.Request(_METADATA_PROJECT_URL, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8").strip()


# --------------------------------------------------------------------------- #
# Vertex AI translation
# --------------------------------------------------------------------------- #

_FINISH_REASONS = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "OTHER": "stop",
}


def to_contents(messages: list) -> tuple[dict | None, list]:
    """OpenAI `messages` -> Vertex `contents` + `systemInstruction`.

    Consecutive same-role turns are merged (Vertex rejects runs of one role less
    predictably than OpenAI does) and a leading assistant turn is preceded by an
    explicit minimal user turn, because Vertex requires the history to start with
    a user turn.
    """
    system_parts: list = []
    contents: list = []
    for raw in messages:
        if not isinstance(raw, dict):
            raise ProxyError(400, E_INVALID, "messages[] entries must be objects",
                             param="messages")
        role = str(raw.get("role") or "").lower()
        content = raw.get("content")
        if isinstance(content, list):  # OpenAI multi-part content
            text = "\n".join(
                str(part.get("text", "")) for part in content
                if isinstance(part, dict) and part.get("type") in (None, "text")
            )
        elif content is None:
            text = ""
        else:
            text = str(content)
        if role == "system":
            system_parts.append({"text": text})
            continue
        vrole = "model" if role in ("assistant", "model") else "user"
        if contents and contents[-1]["role"] == vrole:
            contents[-1]["parts"].append({"text": text})
        else:
            contents.append({"role": vrole, "parts": [{"text": text}]})

    if not contents:
        raise ProxyError(400, E_INVALID, "messages must contain at least one user turn",
                         param="messages")
    if contents[0]["role"] != "user":
        # Protocol shim, not invented content: Vertex requires a leading user turn.
        contents.insert(0, {"role": "user", "parts": [{"text": " "}]})

    system = {"parts": system_parts} if system_parts else None
    return system, contents


def _int_or_none(value, minimum: int = 0, maximum: int | None = None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = int(value)
    if out < minimum:
        out = minimum
    if maximum is not None and out > maximum:
        out = maximum
    return out


def to_generation_config(cfg: Config, body: dict) -> tuple[dict, dict]:
    """OpenAI sampling parameters -> Vertex `generationConfig` + the raw echo.

    `max_tokens` and `temperature` are honoured (contract §10.1: default 1024 and
    0.2 when the caller omits them).
    """
    max_tokens = _int_or_none(body.get("max_tokens"), 1, 512 * 1024)
    if max_tokens is None:
        max_tokens = _int_or_none(body.get("max_completion_tokens"), 1, 512 * 1024)
    if max_tokens is None:
        max_tokens = DEFAULT_MAX_TOKENS

    temperature = body.get("temperature")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        temperature = DEFAULT_TEMPERATURE
    temperature = max(0.0, min(2.0, float(temperature)))

    config: dict = {"temperature": temperature, "maxOutputTokens": max_tokens, "candidateCount": 1}

    top_p = body.get("top_p")
    if isinstance(top_p, (int, float)) and not isinstance(top_p, bool):
        config["topP"] = max(0.0, min(1.0, float(top_p)))

    stop = body.get("stop")
    if isinstance(stop, str):
        stop = [stop]
    if isinstance(stop, list) and stop and len(stop) <= 4:
        config["stopSequences"] = [str(s) for s in stop]

    seed = body.get("seed")
    if isinstance(seed, int) and not isinstance(seed, bool):
        config["seed"] = seed

    fmt = body.get("response_format")
    if isinstance(fmt, dict) and fmt.get("type") in ("json_object", "json_schema"):
        config["responseMimeType"] = "application/json"

    if cfg.gemini_thinking_budget >= 0:
        config["thinkingConfig"] = {"thinkingBudget": cfg.gemini_thinking_budget}

    return config, {"max_tokens": max_tokens, "temperature": temperature}


# --------------------------------------------------------------------------- #
# The proxy handler
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "termini-inference-proxy/1.0"

    # Injected by make_server(); declared here for type checkers.
    cfg: Config = field(default_factory=Config)  # type: ignore[assignment]
    request_log: RequestLog = None  # type: ignore[assignment]

    # -- plumbing ----------------------------------------------------------- #
    def log_message(self, format, *args) -> None:  # noqa: A002 - http.server API
        return  # structured logging only

    def _request_id(self) -> str:
        rid = (self.headers.get("X-Termini-Request-Id") or "").strip()
        return rid or new_id("req-")

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        data = bytearray()
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            data.extend(chunk)
            remaining -= len(chunk)
        return bytes(data)

    def _label_headers(self) -> dict:
        cfg = self.cfg
        headers = {
            "X-Termini-Backend": cfg.backend,
            "X-Termini-Answered-By": cfg.answered_by,
            "X-Termini-Model": cfg.alias,          # the alias the caller asked for
            "X-Termini-Model-Id": cfg.served_model,  # what actually produced tokens
            "X-Termini-Egress": cfg.egress,
            "X-Termini-Residency": "resident",
        }
        if cfg.backend == BACKEND_GEMINI:
            headers["X-Termini-External-Destination"] = cfg.external_destination()
        return headers

    def _send_json(self, status: int, payload: dict, request_id: str,
                   extra: dict | None = None) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in self._label_headers().items():
            self.send_header(key, value)
        self.send_header("X-Termini-Request-Id", request_id)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, err: ProxyError, request_id: str) -> None:
        extra = {}
        if err.retry_after is not None:
            extra["Retry-After"] = str(err.retry_after)
        self._send_json(err.status, error_body(self.cfg, request_id, err), request_id, extra)

    def _begin_sse(self, request_id: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        for key, value in self._label_headers().items():
            self.send_header(key, value)
        self.send_header("X-Termini-Request-Id", request_id)
        self.end_headers()

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def _sse(self, payload: dict) -> None:
        self._write_chunk(b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n")

    def _sse_comment(self, text: str) -> None:
        self._write_chunk(f": {text}\n\n".encode())

    def _sse_done(self) -> None:
        self._write_chunk(b"data: [DONE]\n\n")
        self._write_chunk(b"")  # terminating zero-length chunk

    # -- routes ------------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        request_id = self._request_id()
        if path in ("/health", "/v1/health"):
            self._health(request_id)
        elif path == "/v1/models":
            self._models(request_id)
        elif self.cfg.backend == BACKEND_LOCAL:
            self._passthrough("GET", request_id, path, b"")
        else:
            self._not_found(request_id, path)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        request_id = self._request_id()
        raw = self._read_body()
        if path == "/v1/chat/completions":
            self._chat_completions(request_id, raw)
        elif self.cfg.backend == BACKEND_LOCAL:
            # Anything else llama.cpp serves (e.g. /completion, /v1/completions):
            # in local mode the proxy must not be in the way. The body is relayed
            # byte-for-byte, never re-serialised.
            self._passthrough("POST", request_id, path, raw)
        else:
            self._not_found(request_id, path)

    # -- endpoints ---------------------------------------------------------- #
    def _local_worker_reachable(self, timeout_s: float = 0.5) -> bool:
        """Is a LOCAL llama.cpp worker actually reachable at `upstream` right now?

        The two surfaces of the split (`docs/contracts/infra-decisions.md` D9) run
        the SAME proxy code with different backends, and only ONE of them has a
        local worker behind it:

          * the agent-facing front door on Node C — the worker is there, so a
            caller can switch this listener to `local` in one command;
          * the provider-only listener on the controller — there is no worker on
            that host at all, and a `/health` that claimed a reachable local path
            would be a false statement about the very thing the split is about.

        So the field is MEASURED, not asserted. Bounded and best-effort: a health
        probe must never become a slow request.
        """
        try:
            host, port = self.cfg.upstream_host_port()
        except (ValueError, OSError):
            return False
        conn = None
        try:
            conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
            conn.request("GET", "/health")
            resp = conn.getresponse()
            resp.read()
            return resp.status < 500
        except Exception:  # noqa: BLE001 — unreachable IS the answer
            return False
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    def _health(self, request_id: str) -> None:
        cfg = self.cfg
        body = {
            "status": "ok",
            "model": cfg.alias,
            "backend": cfg.backend,
            "answered_by": cfg.answered_by,
            "egress": cfg.egress,
            "served_model": cfg.served_model,
            "local_path": {
                "available": self._local_worker_reachable(),
                "upstream": cfg.upstream,
                "switch": "TERMINI_INFERENCE_BACKEND=local",
            },
        }
        if cfg.backend == BACKEND_GEMINI:
            body["destination"] = cfg.external_destination()
            body["note"] = (
                "forwarded managed model: requests are answered by "
                f"{cfg.gemini_model} at {cfg.external_destination()}, not by local weights"
            )
        else:
            body["destination"] = cfg.upstream
            body["note"] = "local weights served by llama.cpp on this node"
        self._send_json(200, body, request_id)

    def _models(self, request_id: str) -> None:
        cfg = self.cfg
        body = {
            "object": "list",
            "data": [
                {
                    "id": cfg.alias,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "termini",
                }
            ],
            # Additive: nodes A/B read `data[].id` exactly as before.
            "termini_backend": {
                "backend": cfg.backend,
                "answered_by": cfg.answered_by,
                "served_model": cfg.served_model,
                "egress": cfg.egress,
                **({"destination": cfg.external_destination()}
                   if cfg.backend == BACKEND_GEMINI else {"upstream": cfg.upstream}),
            },
        }
        self._send_json(200, body, request_id)

    def _not_found(self, request_id: str, path: str) -> None:
        err = ProxyError(404, E_MODEL_NOT_FOUND,
                         f"no route {path!r} on the {self.cfg.backend} backend",
                         detail=("this is the forwarded backend: only /health, /v1/models and "
                                 "/v1/chat/completions are served"))
        self._send_error(err, request_id)

    # -- local passthrough -------------------------------------------------- #
    def _passthrough(self, method: str, request_id: str, path: str, body: bytes) -> None:
        """Relay a request to the local llama.cpp worker and relay the answer back.

        Byte-transparent on purpose: in `local` mode the caller is talking to
        llama.cpp, and the only thing this proxy adds is the label header and its
        own log line. Nothing is re-serialised.
        """
        cfg = self.cfg
        started = time.monotonic()
        try:
            host, port = cfg.upstream_host_port()
            conn = http.client.HTTPConnection(host, port, timeout=cfg.upstream_timeout_s)
            headers = {"Accept": self.headers.get("Accept", "*/*")}
            if body:
                headers["Content-Type"] = self.headers.get("Content-Type", "application/json")
            conn.request(method, cfg.upstream_url(path), body=body or None, headers=headers)
            resp = conn.getresponse()
        except (socket.timeout, TimeoutError) as exc:
            self._local_failure(request_id, "timeout", started, str(exc))
            return
        except OSError as exc:
            self._local_failure(request_id, "unreachable", started, str(exc))
            return

        is_stream = resp.getheader("Content-Type", "").startswith("text/event-stream")
        if is_stream:
            self._begin_sse(request_id)
            try:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    self._write_chunk(chunk)
                self._write_chunk(b"")  # terminating zero-length chunk
            except (BrokenPipeError, ConnectionResetError):  # client went away
                pass
            finally:
                conn.close()
            self.request_log.request(
                path=path, stream=True, status=200, request_id=request_id, forwarded=False,
                latency_ms=round((time.monotonic() - started) * 1000, 1),
                upstream=cfg.upstream,
            )
            return

        data = resp.read()
        status = resp.status
        self.send_response(status)
        self.send_header("Content-Type", resp.getheader("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(data)))
        for key, value in self._label_headers().items():
            self.send_header(key, value)
        self.send_header("X-Termini-Request-Id", request_id)
        self.end_headers()
        self.wfile.write(data)
        conn.close()
        self.request_log.request(
            path=path, stream=False, status=status, request_id=request_id, forwarded=False,
            latency_ms=round((time.monotonic() - started) * 1000, 1),
            upstream=cfg.upstream,
        )

    def _local_failure(self, request_id: str, distinct: str, started: float,
                       detail: str) -> None:
        cfg = self.cfg
        self.request_log.failure(
            distinct=f"local_{distinct}", status=503, request_id=request_id,
            latency_ms=round((time.monotonic() - started) * 1000, 1),
            upstream=cfg.upstream, detail=detail[:400],
        )
        err = ProxyError(
            503, E_UNAVAILABLE,
            f"local backend {cfg.upstream} is {distinct}",
            detail=detail[:400], retry_after=5, distinct=f"local_{distinct}",
        )
        self._send_error(err, request_id)

    # -- forwarded backend -------------------------------------------------- #
    def _chat_completions(self, request_id: str, raw: bytes) -> None:
        cfg = self.cfg
        if cfg.backend == BACKEND_LOCAL:
            # The local path must be llama.cpp, exactly: relay the caller's bytes.
            self._passthrough("POST", request_id, "/v1/chat/completions", raw)
            return

        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            self._send_error(ProxyError(400, E_INVALID, f"invalid JSON body: {exc}"), request_id)
            return
        if not isinstance(body, dict):
            self._send_error(ProxyError(400, E_INVALID, "body must be a JSON object"), request_id)
            return

        try:
            system, contents = to_contents(body.get("messages") or [])
            generation, echo = to_generation_config(cfg, body)
        except ProxyError as err:
            self.request_log.failure(distinct="bad_request", status=err.status,
                                     request_id=request_id, detail=err.message)
            self._send_error(err, request_id)
            return

        payload: dict = {"contents": contents, "generationConfig": generation}
        if system is not None:
            payload["systemInstruction"] = system

        include_usage = bool(
            isinstance(body.get("stream_options"), dict)
            and body["stream_options"].get("include_usage")
        )
        if bool(body.get("stream", False)):
            self._forward_stream(request_id, payload, echo, include_usage)
        else:
            self._forward_once(request_id, payload, echo)

    def _vertex_call(self, payload: dict, stream: bool):
        cfg = self.cfg
        try:
            token = cfg.access_token or metadata_token()
        except Exception as exc:  # noqa: BLE001 - loud, distinct, never silent
            raise ProxyError(
                503, E_UNAVAILABLE,
                "cannot mint an access token from the node's metadata server",
                detail=f"{type(exc).__name__}: {exc}"[:400],
                distinct="metadata_token") from exc
        if not cfg.gemini_project:
            cfg.gemini_project = metadata_project()
        if not cfg.gemini_project:
            raise ProxyError(503, E_UNAVAILABLE,
                             "forwarded backend has no Google Cloud project configured",
                             detail="set TERMINI_GEMINI_PROJECT", distinct="no_project")
        req = urllib.request.Request(
            cfg.generate_url(stream),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=cfg.gemini_timeout_s)

    def _map_upstream_error(self, exc: Exception) -> ProxyError:
        cfg = self.cfg
        dest = cfg.external_destination()
        if isinstance(exc, urllib.error.HTTPError):
            status = exc.code
            detail = ""
            try:
                raw = exc.read().decode("utf-8", "replace")
                parsed = json.loads(raw)
                detail = str(parsed.get("error", {}).get("message", raw))[:400]
            except Exception:  # noqa: BLE001 - the status is the load-bearing part
                pass
            retry_after = None
            header = exc.headers.get("Retry-After") if exc.headers else None
            if header:
                try:
                    retry_after = int(float(header))
                except ValueError:
                    retry_after = None
            if status == 429:
                return ProxyError(
                    429, E_BUSY,
                    f"forwarded backend {cfg.gemini_model} at {dest} is rate limited",
                    detail=detail, retry_after=retry_after or 5, distinct="vertex_rate_limited")
            if status in (401, 403):
                return ProxyError(
                    503, E_UNAVAILABLE,
                    f"forwarded backend {cfg.gemini_model} at {dest} refused this node's identity",
                    detail=detail, distinct="vertex_auth")
            if status in (400, 404):
                return ProxyError(
                    503, E_UNAVAILABLE,
                    f"forwarded backend {cfg.gemini_model} at {dest} rejected the request",
                    detail=detail, distinct="vertex_rejected")
            return ProxyError(
                503, E_UNAVAILABLE,
                f"forwarded backend {cfg.gemini_model} at {dest} returned HTTP {status}",
                detail=detail, retry_after=retry_after, distinct="vertex_unavailable")
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return ProxyError(
                504, E_TIMEOUT,
                f"forwarded backend {cfg.gemini_model} at {dest} timed out after "
                f"{cfg.gemini_timeout_s:g}s",
                detail="the request path is bounded on purpose: the caller must fall back",
                distinct="vertex_timeout")
        if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, (socket.timeout, TimeoutError)):
            return ProxyError(
                504, E_TIMEOUT,
                f"forwarded backend {cfg.gemini_model} at {dest} timed out",
                detail=str(exc.reason), distinct="vertex_timeout")
        if isinstance(exc, (urllib.error.URLError, OSError)):
            return ProxyError(
                503, E_UNAVAILABLE,
                f"forwarded backend {cfg.gemini_model} at {dest} is unreachable",
                detail=str(getattr(exc, "reason", exc))[:400], distinct="vertex_unreachable")
        return ProxyError(500, E_ERROR, "forwarded backend failed",
                          detail=f"{type(exc).__name__}: {exc}"[:400], distinct="vertex_error")

    # -- non-streaming forward --------------------------------------------- #
    def _forward_once(self, request_id: str, payload: dict, echo: dict) -> None:
        cfg = self.cfg
        dest = cfg.external_destination()
        started = time.monotonic()
        try:
            resp = self._vertex_call(payload, stream=False)
            body = json.loads(resp.read().decode("utf-8"))
        except ProxyError as err:
            self._upstream_failure(err, request_id, started, stream=False)
            return
        except Exception as exc:  # noqa: BLE001 - mapped, then logged loudly
            err = self._map_upstream_error(exc)
            self._upstream_failure(err, request_id, started, stream=False)
            return

        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        text, finish, usage = _read_generate_response(body)
        payload_out = {
            "id": new_id(),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": cfg.alias,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": finish,
                }
            ],
            "usage": usage,
            "timings": _timings(usage, elapsed_ms),
            # The in-band substitution label (requirement: never claim local/GPU).
            "termini_backend": self._label_body(),
        }
        self._send_json(200, payload_out, request_id)
        self.request_log.request(
            path="/v1/chat/completions", stream=False, status=200, request_id=request_id,
            latency_ms=elapsed_ms, destination=dest, forwarded=True,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            requested_max_tokens=echo.get("max_tokens"),
            requested_temperature=echo.get("temperature"),
            finish_reason=finish,
        )

    # -- streaming forward -------------------------------------------------- #
    def _forward_stream(self, request_id: str, payload: dict, echo: dict,
                        include_usage: bool) -> None:
        cfg = self.cfg
        dest = cfg.external_destination()
        started = time.monotonic()
        try:
            resp = self._vertex_call(payload, stream=True)
        except ProxyError as err:
            self._upstream_failure(err, request_id, started, stream=True)
            return
        except Exception as exc:  # noqa: BLE001
            err = self._map_upstream_error(exc)
            self._upstream_failure(err, request_id, started, stream=True)
            return

        self._begin_sse(request_id)
        self._sse({
            "id": new_id(), "object": "chat.completion.chunk",
            "created": int(time.time()), "model": cfg.alias,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        })
        first_token_at = None
        text_len = 0
        finish = "stop"
        usage: dict = {}
        try:
            # No blind keep-alive loop here: the upstream read is bounded by
            # `TERMINI_GEMINI_TIMEOUT_S` (30 s by default, i.e. the contract's
            # first-token deadline), and a stall surfaces as a 504 INFERENCE_TIMEOUT
            # error chunk rather than an open stream that never answers.
            for frame in _iter_sse(resp):
                if frame is None:  # keep-alive from upstream
                    continue
                chunk_text, chunk_finish, chunk_usage = _read_generate_response(frame)
                if chunk_usage:
                    usage = chunk_usage
                if chunk_finish:
                    finish = chunk_finish
                if not chunk_text:
                    continue
                if first_token_at is None:
                    first_token_at = time.monotonic()
                text_len += len(chunk_text)
                self._sse({
                    "id": new_id(), "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": cfg.alias,
                    "choices": [{"index": 0, "delta": {"content": chunk_text},
                                 "finish_reason": None}],
                })
        except Exception as exc:  # noqa: BLE001 - mid-stream failure
            err = self._map_upstream_error(exc)
            self.request_log.failure(
                distinct=err.distinct or "vertex_error", status=err.status,
                request_id=request_id, stream=True, forwarded=True,
                destination=dest, detail=err.detail,
                latency_ms=round((time.monotonic() - started) * 1000, 1),
            )
            self._sse({"error": {
                "message": err.message, "type": "termini_error", "code": err.code,
                "request_id": request_id, "backend": cfg.backend,
                "destination": dest, "detail": err.detail,
            }})
            self._sse_done()
            return
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        if usage:
            usage = dict(usage)
        else:
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        final: dict = {
            "id": new_id(), "object": "chat.completion.chunk",
            "created": int(time.time()), "model": cfg.alias,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            "timings": _timings(usage, elapsed_ms),
        }
        if include_usage:
            final["usage"] = usage
        final["termini_backend"] = self._label_body()
        self._sse(final)
        self._sse_done()
        self.request_log.request(
            path="/v1/chat/completions", stream=True, status=200, request_id=request_id,
            latency_ms=elapsed_ms, destination=dest, forwarded=True,
            first_token_ms=(round((first_token_at - started) * 1000, 1)
                            if first_token_at else None),
            completion_chars=text_len,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            requested_max_tokens=echo.get("max_tokens"),
            requested_temperature=echo.get("temperature"),
            finish_reason=finish,
        )

    def _upstream_failure(self, err: ProxyError, request_id: str, started: float,
                          stream: bool) -> None:
        cfg = self.cfg
        self.request_log.failure(
            distinct=err.distinct or "vertex_error", status=err.status,
            request_id=request_id, stream=stream, forwarded=True,
            destination=cfg.external_destination(), detail=err.detail or err.message,
            latency_ms=round((time.monotonic() - started) * 1000, 1),
        )
        self._send_error(err, request_id)

    # -- labelling ---------------------------------------------------------- #
    def _label_body(self) -> dict:
        cfg = self.cfg
        label: dict = {
            "backend": cfg.backend,
            "answered_by": cfg.answered_by,
            "model": cfg.served_model,
            "alias": cfg.alias,
            "egress": cfg.egress,
        }
        if cfg.backend == BACKEND_GEMINI:
            label["destination"] = cfg.external_destination()
            label["forwarded"] = True
            label["note"] = ("answered by a forwarded managed model, not by local weights "
                             "on this node and not by an accelerator on this node")
        else:
            label["forwarded"] = False
            label["upstream"] = cfg.upstream
        return label


def _read_generate_response(body: dict) -> tuple[str, str, dict]:
    """Extract (text, finish_reason, usage) from a Vertex GenerateContentResponse."""
    text = ""
    finish = ""
    candidates = body.get("candidates") or []
    if candidates and isinstance(candidates[0], dict):
        cand = candidates[0]
        for part in (cand.get("content") or {}).get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text += part["text"]
        reason = cand.get("finishReason")
        if isinstance(reason, str) and reason:
            finish = _FINISH_REASONS.get(reason, "stop")
    usage: dict = {}
    meta = body.get("usageMetadata")
    if isinstance(meta, dict):
        prompt = _int_or_none(meta.get("promptTokenCount"), 0) or 0
        completion = _int_or_none(meta.get("candidatesTokenCount"), 0) or 0
        total = _int_or_none(meta.get("totalTokenCount"), 0) or (prompt + completion)
        usage = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
        }
    return text, finish, usage


def _timings(usage: dict, elapsed_ms: float) -> dict:
    """llama.cpp-shaped `timings`, so a dashboard reading them keeps working."""
    prompt = usage.get("prompt_tokens") or 0
    predicted = usage.get("completion_tokens") or 0
    return {
        "prompt_n": prompt,
        "prompt_ms": 0.0,
        "predicted_n": predicted,
        "predicted_ms": elapsed_ms,
        "predicted_per_token_ms": round(elapsed_ms / predicted, 3) if predicted else 0.0,
        "predicted_per_second": round(predicted / (elapsed_ms / 1000.0), 3) if elapsed_ms else 0.0,
        "wall_ms": elapsed_ms,
    }


def _iter_sse(resp):
    """Yield decoded SSE JSON frames from a streaming Vertex response."""
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        if line.startswith(":"):  # a comment / keep-alive
            yield None
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


# --------------------------------------------------------------------------- #
# Server plumbing
# --------------------------------------------------------------------------- #

class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(cfg: Config, *, host: str | None = None, port: int | None = None,
                log_stream=None) -> Server:
    request_log = RequestLog(cfg, stream=log_stream)
    handler = type("BoundHandler", (Handler,), {"cfg": cfg, "request_log": request_log})
    bind_host = cfg.host if host is None else host
    bind_port = cfg.port if port is None else port
    return Server((bind_host, bind_port), handler)


def write_state(cfg: Config, active_unit: str, note: str = "") -> str:
    """Record which backend is ACTIVE on this node (the node's own status file)."""
    state = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "backend": cfg.backend,
        "active_unit": active_unit,
        "alias": cfg.alias,
        "served_model": cfg.served_model,
        "answered_by": cfg.answered_by,
        "egress": cfg.egress,
        "listen": f"{cfg.host}:{cfg.port}",
        "local_worker": cfg.upstream,
        "destination": (cfg.external_destination() if cfg.backend == BACKEND_GEMINI
                        else cfg.upstream),
        "is_gpu_inference": False,
        "note": note or (
            "forwarded managed model emulating a GPU node" if cfg.backend == BACKEND_GEMINI
            else "local CPU weights (llama.cpp) on this node"
        ),
    }
    path = cfg.state_file
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except OSError as exc:  # pragma: no cover - unwritable /run on odd hosts
        print(f"termini-inference-proxy: cannot write state {path}: {exc}",
              file=sys.stderr, flush=True)
    return path


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Termini Node C inference proxy (drop-in)")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--backend", default=None, choices=BACKENDS)
    parser.add_argument("--upstream", default=None)
    parser.add_argument("--state-file", default=None)
    parser.add_argument("--check", action="store_true",
                        help="print the effective configuration and exit")
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    if args.host:
        cfg.host = args.host
    if args.port is not None:
        cfg.port = args.port
    if args.backend:
        cfg.backend = args.backend
    if args.upstream:
        cfg.upstream = args.upstream
    if args.state_file:
        cfg.state_file = args.state_file

    log = RequestLog(cfg)
    if args.check:
        print(json.dumps({
            "backend": cfg.backend, "listen": f"{cfg.host}:{cfg.port}",
            "alias": cfg.alias, "served_model": cfg.served_model,
            "answered_by": cfg.answered_by, "egress": cfg.egress,
            "upstream": cfg.upstream,
            "destination": (cfg.external_destination() if cfg.backend == BACKEND_GEMINI
                            else cfg.upstream),
        }, indent=2))
        return 0

    state_path = write_state(cfg, "termini-inference-proxy.service")
    log.emit("startup", listen=f"{cfg.host}:{cfg.port}", state_file=state_path,
             upstream=cfg.upstream, project=cfg.gemini_project or "(metadata)",
             thinking_budget=cfg.gemini_thinking_budget)
    server = make_server(cfg)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
