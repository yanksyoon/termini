"""In-VM task surface — hand this agent a task and read back its answer.

Contract: `docs/contracts/agent-runtime.md` §5.6 (the task-submit surface) and
§7.4 (task states + failure taxonomy). Card `t_1a2c913c`.

Three properties are the whole point of this module, and each is enforced here
rather than documented-and-hoped:

  * **Localhost-bound.** It is served on the runtime's existing loopback listener
    (`127.0.0.1:9101`, §5.1) — the same surface `/status` uses. Nothing here is
    reachable from outside the instance; the only way in is the node's control
    plane driving an `exec` inside it (§7.4, decision AT-1).

  * **Bounded.** An instance admits at most `tasks.max_concurrent` tasks at once
    (default 2, inherited from `broker.max_concurrent_tasks`). A submission while
    the bound is reached is refused immediately with `429 TASK_BOUND_EXCEEDED`
    and a `Retry-After` — it is never queued invisibly, so a task cannot saturate
    the node and an over-bound submission is distinguishable from a slow model.

  * **Never a plausible lie.** A task reaches `done` only with non-empty content
    from the configured on-prem endpoint. Every other outcome is `error` with a
    distinct `code` from the §10.5 family that names *what* failed: an
    unreachable endpoint is `INFERENCE_UNREACHABLE`, a refused alias is
    `MODEL_NOT_ALLOWED`, a first-token/read deadline is `INFERENCE_TIMEOUT`, a
    truncated-to-nothing answer is `EMPTY_COMPLETION`. A timeout never reads as
    success, and the provenance of whatever did answer (which backend, whether it
    was forwarded off-premises) is carried on the result so no caller has to
    guess or hardcode it.

The answer body is the customer's own content (C2): it is written to the on-prem
result sink (`/var/lib/termini/agent/results/<task_id>.json`, 0600, §7.3) and is
never written to a log line — log lines carry the C0 counters only.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .inference import (
    Completion,
    EmptyCompletion,
    InferenceClient,
    InferenceError,
)
from .ulid import new_ulid

log = logging.getLogger("termini_agent.taskapi")

# Task states (agent-runtime.md §7.4). `done`/`error` are terminal.
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
ERROR = "error"
TERMINAL = (DONE, ERROR)

# Bounds (all recorded in the contract).
DEFAULT_MAX_CONCURRENT = 2
DEFAULT_MAX_PROMPT_BYTES = 8192
DEFAULT_MAX_TOKENS = 256
MAX_TOKENS_CEILING = 4096
DEFAULT_RETENTION = 64

# What the operator should do about each failure code — carried on the error so
# the caller gets an actionable answer, not just a code.
_REMEDIES = {
    "INFERENCE_UNREACHABLE": (
        "the on-prem inference endpoint is not answering at all: check that node C "
        "is up and that inference.base_url names its underlay address"
    ),
    "INFERENCE_TIMEOUT": (
        "the endpoint accepted the call but did not answer within "
        "inference.request_timeout_s: retry, or lower max_tokens/length"
    ),
    "MODEL_UNAVAILABLE": "the endpoint reports no usable backend for this alias",
    "INFERENCE_BUSY": "the endpoint is saturated: retry after the Retry-After window",
    "MODEL_NOT_ALLOWED": (
        "the alias is not in this agent's inference.allowed_models: the config must "
        "name the alias the endpoint actually serves"
    ),
    "MODEL_NOT_FOUND": "the endpoint does not serve this alias: check the agent config",
    "CONTEXT_LENGTH_EXCEEDED": "prompt + max_tokens does not fit the alias context",
    "EMPTY_COMPLETION": (
        "the endpoint returned success with empty content: treat as a failure, never "
        "as an empty answer"
    ),
    "TASK_BOUND_EXCEEDED": (
        "this instance's task bound is already in flight: wait for a running task to "
        "finish (Retry-After) or use another instance"
    ),
    "INFERENCE_CLIENT_UNAVAILABLE": "the in-VM inference client could not be built",
    "INFERENCE_ERROR": "unclassified inference failure: see the endpoint's own logs",
}


class TaskError(RuntimeError):
    """A refusal from the task surface (mapped to the §10.5 error envelope)."""

    code = "TASK_ERROR"
    status = 400

    def __init__(self, message: str, *, code: str | None = None,
                 status: int | None = None, param: str | None = None,
                 retry_after: int | None = None):
        super().__init__(message)
        if code:
            self.code = code
        if status:
            self.status = status
        self.param = param
        self.retry_after = retry_after


class TaskBoundExceeded(TaskError):
    code = "TASK_BOUND_EXCEEDED"
    status = 429


class TaskNotFound(TaskError):
    code = "TASK_NOT_FOUND"
    status = 404


class TaskInvalid(TaskError):
    code = "INVALID_ARGUMENT"
    status = 400


class TaskTooLarge(TaskError):
    code = "PROMPT_TOO_LARGE"
    status = 413


@dataclass
class TaskRecord:
    """One submitted task and (eventually) its answer."""

    task_id: str
    prompt: str
    state: str = QUEUED
    actor: str | None = None
    model_preference: str | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = 0.2
    submitted_at: float = 0.0
    started_at: float | None = None
    ended_at: float | None = None
    duration_ms: int | None = None
    model: str | None = None
    answer: str | None = None
    tokens: int | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    result_ref: str | None = None

    def as_dict(self, *, include_answer: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "task_id": self.task_id,
            "state": self.state,
            "actor": self.actor,
            "model_preference": self.model_preference,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "submitted_at": _iso(self.submitted_at),
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
            "duration_ms": self.duration_ms,
            "tokens": self.tokens,
            "usage": dict(self.usage),
            "provenance": dict(self.provenance),
            "error": dict(self.error) if self.error else None,
            "result_ref": self.result_ref,
        }
        if include_answer:
            # The answer is the caller's own content; it is only ever returned on
            # the localhost surface (never logged, never egressed by the runtime).
            out["answer"] = self.answer
        return out


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    import datetime

    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _error_envelope(code: str | None, message: str, *, param: str | None = None,
                    actionable: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code or "TASK_ERROR", "message": message}
    if param:
        body["param"] = param
    if actionable:
        body["actionable"] = actionable
    return body


class TaskService:
    """Bounded, in-process task queue for one agent runtime."""

    def __init__(self, cfg, *, client: "InferenceClient | Any | None" = None,
                 bound: int | None = None, clock=time.time,
                 max_prompt_bytes: int = DEFAULT_MAX_PROMPT_BYTES,
                 retention: int = DEFAULT_RETENTION,
                 result_sink: str | None = None):
        self.cfg = cfg
        self.clock = clock
        self.max_prompt_bytes = int(max_prompt_bytes)
        self.retention = int(retention)
        tasks_cfg = getattr(cfg, "tasks", None)
        self.bound = int(
            bound
            if bound is not None
            else getattr(tasks_cfg, "max_concurrent", None)
            or cfg.broker.max_concurrent_tasks
        )
        self.result_sink = Path(
            result_sink
            or getattr(tasks_cfg, "result_sink", None)
            or "/var/lib/termini/agent/results"
        )
        self._client = client
        self._client_error: str | None = None
        self._records: "OrderedDict[str, TaskRecord]" = OrderedDict()
        self._lock = threading.Lock()
        self._in_flight = 0
        self.completed = 0
        self.failed = 0
        self.last_task_id: str | None = None

    # -- client ---------------------------------------------------------------
    @property
    def client(self) -> InferenceClient:
        """Lazily built so an unusable client is a task error, not a boot failure."""
        if self._client is None:
            if self._client_error:
                raise InferenceError(self._client_error)
            try:
                self._client = InferenceClient(self.cfg)
            except InferenceError as exc:  # pragma: no cover - image always has `openai`
                self._client_error = str(exc)
                raise
        return self._client

    # -- public surface -------------------------------------------------------
    def submit(self, body: dict[str, Any]) -> dict[str, Any]:
        """Validate, admit and start one task. Raises TaskError on refusal."""
        if not isinstance(body, dict):
            raise TaskInvalid("body must be a JSON object")

        prompt = body.get("prompt")
        messages = body.get("messages")
        if prompt is None and not messages:
            raise TaskInvalid("prompt is required", param="prompt")
        if prompt is not None and not isinstance(prompt, str):
            raise TaskInvalid("prompt must be a string", param="prompt")
        if messages is not None:
            if not isinstance(messages, list) or not messages:
                raise TaskInvalid("messages must be a non-empty array", param="messages")
            for m in messages:
                if not isinstance(m, dict) or not isinstance(m.get("content"), str):
                    raise TaskInvalid("each message needs a string content",
                                      param="messages")
        if prompt is not None:
            size = len(prompt.encode("utf-8"))
            if size > self.max_prompt_bytes:
                raise TaskTooLarge(
                    f"prompt is {size} bytes; the per-task limit is "
                    f"{self.max_prompt_bytes}",
                    param="prompt",
                )

        max_tokens = body.get("max_tokens", DEFAULT_MAX_TOKENS)
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
            raise TaskInvalid("max_tokens must be a positive integer", param="max_tokens")
        if max_tokens > MAX_TOKENS_CEILING:
            raise TaskInvalid(
                f"max_tokens must be ≤ {MAX_TOKENS_CEILING}", param="max_tokens")
        temperature = body.get("temperature", 0.2)
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) \
                or not (0.0 <= float(temperature) <= 2.0):
            raise TaskInvalid("temperature must be a number in [0, 2]", param="temperature")
        actor = body.get("actor")
        if actor is not None and not isinstance(actor, str):
            raise TaskInvalid("actor must be a string", param="actor")

        preference = body.get("model_preference") or body.get("model")
        if preference is not None and not isinstance(preference, str):
            raise TaskInvalid("model_preference must be a string", param="model_preference")
        if preference is not None:
            # Fail fast on an alias this instance may not use (AR-6): a retired
            # catalog alias is refused NOW with MODEL_NOT_ALLOWED, never queued and
            # then failed later, and never silently substituted. A client that
            # cannot even be built is reported by the worker instead of blocking
            # the submission.
            try:
                self.client.resolve_model(preference)
            except InferenceError as exc:
                if getattr(exc, "code", "") == "MODEL_NOT_ALLOWED":
                    raise TaskInvalid(
                        str(exc), code="MODEL_NOT_ALLOWED", status=400,
                        param="model_preference") from exc

        record = TaskRecord(
            task_id=new_ulid(),
            prompt=prompt if prompt is not None else _flatten(messages),
            actor=actor,
            model_preference=preference,
            max_tokens=int(max_tokens),
            temperature=float(temperature),
            submitted_at=self.clock(),
        )
        if prompt is None:
            record_messages = [dict(m) for m in messages]  # type: ignore[union-attr]
        else:
            record_messages = [{"role": "user", "content": prompt}]

        with self._lock:
            if self._in_flight >= self.bound:
                raise TaskBoundExceeded(
                    f"this instance's task bound is reached: {self._in_flight} of "
                    f"{self.bound} task(s) in flight (tasks.max_concurrent={self.bound})",
                    retry_after=5,
                )
            self._in_flight += 1
            self._records[record.task_id] = record
            self.last_task_id = record.task_id
            while len(self._records) > self.retention:
                self._records.popitem(last=False)

        log.info("task accepted", extra={"termini": {
            "task_id": record.task_id, "state": QUEUED, "actor": actor,
            "max_tokens": record.max_tokens,
            "prompt_bytes": len(record.prompt.encode("utf-8")),
            "in_flight": self._in_flight}})
        # Snapshot the ACCEPTANCE state before the worker can move it to `running`,
        # so the submitter always gets the same answer about what it was given.
        accepted = record.as_dict(include_answer=False)
        thread = threading.Thread(target=self._run, args=(record, record_messages),
                                  name=f"task-{record.task_id[-6:]}", daemon=True)
        thread.start()
        return accepted

    def get(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._records.get(task_id)
        if record is None:
            raise TaskNotFound(f"no task {task_id} on this instance")
        return record.as_dict()

    def list(self, limit: int = 20) -> dict[str, Any]:
        limit = max(1, min(int(limit), self.retention))
        with self._lock:
            records = list(self._records.values())[-limit:]
        return {"tasks": [r.as_dict(include_answer=False) for r in reversed(records)],
                "count": len(records), "bound": self.bound}

    def counters(self) -> dict[str, Any]:
        """C0-only view folded into the runtime's /status (§13.2)."""
        with self._lock:
            states = {QUEUED: 0, RUNNING: 0, DONE: 0, ERROR: 0}
            for rec in self._records.values():
                states[rec.state] = states.get(rec.state, 0) + 1
            in_flight = self._in_flight
        return {
            "bound": self.bound,
            "in_flight": in_flight,
            "queued": states[QUEUED],
            "completed": states[DONE],
            "failed": states[ERROR],
            "tasks_completed_total": self.completed,
            "tasks_failed_total": self.failed,
            "last_task_id": self.last_task_id,
        }

    # -- worker ---------------------------------------------------------------
    def _run(self, record: TaskRecord, messages: Sequence[dict[str, str]]) -> None:
        started = self.clock()
        with self._lock:
            record.state = RUNNING
            record.started_at = started
        log.info("task running", extra={"termini": {
            "task_id": record.task_id, "state": RUNNING}})
        try:
            client = self.client
            model = client.resolve_model(record.model_preference)
            completion: Completion = client.complete_with_provenance(
                messages, model=model, max_tokens=record.max_tokens,
                temperature=record.temperature,
            )
            if not completion.content or not completion.content.strip():
                raise EmptyCompletion(
                    "the endpoint answered successfully with empty content")
            with self._lock:
                record.answer = completion.content
                record.model = completion.model
                record.provenance = dict(completion.provenance)
                record.usage = dict(completion.usage)
                record.tokens = int(
                    completion.usage.get("completion_tokens")
                    or max(1, len(completion.content) // 4)
                )
                record.result_ref = self._write_result(record)
                record.state = DONE
            with self._lock:
                self.completed += 1
            log.info("task done", extra={"termini": {
                "task_id": record.task_id, "state": DONE, "model": record.model,
                "tokens": record.tokens, "backend": record.provenance.get("backend")}})
        except InferenceError as exc:
            code = getattr(exc, "code", "INFERENCE_ERROR")
            with self._lock:
                record.state = ERROR
                record.error = _error_envelope(
                    code, str(exc), actionable=_REMEDIES.get(code))
            with self._lock:
                self.failed += 1
            log.warning("task failed", extra={"termini": {
                "task_id": record.task_id, "state": ERROR, "code": code}})
        except Exception as exc:  # noqa: BLE001 — a task must never kill the runtime
            with self._lock:
                record.state = ERROR
                record.error = _error_envelope(
                    "TASK_ERROR", f"{type(exc).__name__}: {exc}",
                    actionable="unexpected runtime failure; the task is not done")
                self.failed += 1
            log.exception("task crashed", extra={"termini": {
                "task_id": record.task_id, "state": ERROR}})
        finally:
            ended = self.clock()
            with self._lock:
                record.ended_at = ended
                record.duration_ms = int((ended - started) * 1000)
                self._in_flight -= 1

    def _write_result(self, record: TaskRecord) -> str | None:
        """Persist the C2 result body to the on-prem sink (§7.3). Best effort."""
        try:
            self.result_sink.mkdir(parents=True, exist_ok=True)
            path = self.result_sink / f"{record.task_id}.json"
            path.write_text(json.dumps({
                "task_id": record.task_id,
                "vm_id": self.cfg.vm_id,
                "project_id": self.cfg.project_id,
                "actor": record.actor,
                "model": record.model,
                "provenance": record.provenance,
                "usage": record.usage,
                "submitted_at": _iso(record.submitted_at),
                "ended_at": _iso(record.ended_at),
                "duration_ms": record.duration_ms,
                "answer": record.answer,
            }), encoding="utf-8")
            os.chmod(path, 0o600)
            return str(path)
        except OSError as exc:  # pragma: no cover - sink is local and writable
            log.warning("result sink unwritable", extra={"termini": {
                "task_id": record.task_id, "error": str(exc)}})
            return None


def _flatten(messages: Any) -> str:
    try:
        return "\n".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))
    except Exception:  # noqa: BLE001
        return ""


def error_envelope(exc: TaskError) -> dict[str, Any]:
    """The §10.5-shaped error body the HTTP surface returns."""
    return {"error": {
        "type": "termini_error",
        "code": exc.code,
        "message": str(exc),
        "param": exc.param,
        "actionable": _REMEDIES.get(exc.code),
        "retry_after_s": exc.retry_after,
    }}
