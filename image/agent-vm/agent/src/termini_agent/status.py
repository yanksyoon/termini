"""Local status surface (agent-runtime.md §5.1).

Two endpoints, both local-only:
  * a unix socket at /run/termini/agent.sock (newline-delimited JSON request/response)
  * an HTTP mirror at 127.0.0.1:9101/status for in-VM tooling

Neither is reachable off the VM; they are how the node daemon samples C0 state.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

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


def _make_handler(state: StatusState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?")[0] in ("/status", "/"):
                self._send(200, state.snapshot())
            else:
                self._send(404, {"error": "not_found"})

        def log_message(self, *args: Any, **kwargs: Any) -> None:  # silence default stderr spam
            return

    return Handler


class StatusServer:
    def __init__(self, state: StatusState, *, http_host: str = "127.0.0.1",
                 http_port: int = 9101, unix_path: str | None = "/run/termini/agent.sock"):
        self.state = state
        self._http = ThreadingHTTPServer((http_host, http_port), _make_handler(state))
        self._http.daemon_threads = True
        self._unix_path = unix_path
        self._unix: socket.socket | None = None
        self._threads: list[threading.Thread] = []

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
