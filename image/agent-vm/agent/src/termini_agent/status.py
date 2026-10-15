"""Local status + task surface (agent-runtime.md §5.1, §5.6).

Two listeners, both local-only:
  * a unix socket at /run/termini/agent.sock (newline-delimited JSON request/response)
  * an HTTP mirror at 127.0.0.1:9101 for in-VM tooling:
      - `GET  /status`            — the C0 status snapshot
      - `POST /v1/tasks`          — give this agent a task (§5.6)
      - `GET  /v1/tasks/{id}`     — read one task's state/result
      - `GET  /v1/tasks`          — enumerate recent tasks

Neither is reachable off the VM; they are how the node daemon samples C0 state and
how its control plane hands the agent work. The task routes exist only when a
`TaskService` is supplied (the runtime always supplies one).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

from . import taskapi

log = logging.getLogger("termini_agent.status")


class StatusState:
    """Thread-shared, read-mostly snapshot of runtime state."""

    def __init__(self, base: dict[str, Any]):
        self._lock = threading.Lock()
        self._base = base

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._base))

    def update(self, **fields: Any) -> None:
        with self._lock:
            self._base.update(fields)


def _make_handler(state: StatusState, tasks: "taskapi.TaskService | None" = None
                  ) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict[str, Any],
                  headers: dict[str, str] | None = None) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        # -- task surface (§5.6) ---------------------------------------------
        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0:
                return {}
            if length > 1 << 20:
                raise taskapi.TaskTooLarge("request body exceeds 1 MiB")
            raw = self.rfile.read(length)
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise taskapi.TaskInvalid(f"malformed JSON body: {exc}") from exc
            if not isinstance(obj, dict):
                raise taskapi.TaskInvalid("body must be a JSON object")
            return obj

        def _task_error(self, exc: taskapi.TaskError) -> None:
            headers = {}
            if getattr(exc, "retry_after", None):
                headers["Retry-After"] = str(exc.retry_after)
            self._send(getattr(exc, "status", 400), taskapi.error_envelope(exc), headers)

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path.rstrip("/") or "/"
            if path != "/v1/tasks" or tasks is None:
                self._send(404, {"error": {"code": "TASK_NOT_FOUND",
                                           "message": f"no route: POST {path}"}})
                return
            try:
                body = self._read_json_body()
                record = tasks.submit(body)
            except taskapi.TaskError as exc:
                self._task_error(exc)
                return
            except Exception as exc:  # noqa: BLE001 - never leak a traceback
                log.exception("task submit crashed")
                self._send(500, {"error": {"code": "TASK_ERROR",
                                           "message": f"{type(exc).__name__}: {exc}"}})
                return
            self._send(202, record, {"X-Termini-Task-Id": record["task_id"]})

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path in ("/status", "/"):
                self._send(200, state.snapshot())
                return
            if tasks is None or not path.startswith("/v1/tasks"):
                self._send(404, {"error": {"code": "TASK_NOT_FOUND",
                                           "message": f"no route: GET {path}"}})
                return
            tail = path[len("/v1/tasks"):].strip("/")
            try:
                if not tail:
                    limit = 20
                    q = parsed.query
                    for part in q.split("&"):
                        if part.startswith("limit="):
                            try:
                                limit = int(part.split("=", 1)[1])
                            except ValueError:
                                limit = 20
                    self._send(200, tasks.list(limit))
                    return
                self._send(200, tasks.get(unquote(tail)))
            except taskapi.TaskError as exc:
                self._task_error(exc)

        def log_message(self, *args: Any, **kwargs: Any) -> None:  # silence default stderr spam
            return

    return Handler


class StatusServer:
    def __init__(self, state: StatusState, *, http_host: str = "127.0.0.1",
                 http_port: int = 9101, unix_path: str | None = "/run/termini/agent.sock",
                 tasks: "taskapi.TaskService | None" = None):
        self.state = state
        self.tasks = tasks
        self._http = ThreadingHTTPServer((http_host, http_port), _make_handler(state, tasks))
        self._http.daemon_threads = True
        self._unix_path = unix_path
        self._unix: socket.socket | None = None
        self._threads: list[threading.Thread] = []

    @property
    def port(self) -> int:
        """The TCP port actually bound (useful when `http_port=0` in tests)."""
        return int(self._http.server_address[1])

    def start(self) -> None:
        t = threading.Thread(target=self._http.serve_forever, name="status-http", daemon=True)
        t.start()
        self._threads.append(t)
        if self._unix_path:
            try:
                self._start_unix()
            except OSError as exc:
                log.warning("unix status socket unavailable", extra={"termini": {"path": self._unix_path, "error": str(exc)}})

    def _start_unix(self) -> None:
        path = self._unix_path
        assert path is not None
        os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
        if os.path.exists(path):
            os.unlink(path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        # owned by the agent user; readable by the group only (0600)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        srv.listen(8)
        self._unix = srv

        def serve() -> None:
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                with conn:
                    conn.sendall(json.dumps(self.state.snapshot()).encode() + b"\n")

        t = threading.Thread(target=serve, name="status-unix", daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self) -> None:
        self._http.shutdown()
        if self._unix is not None:
            try:
                self._unix.close()
            finally:
                if self._unix_path and os.path.exists(self._unix_path):
                    os.unlink(self._unix_path)
