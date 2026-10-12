"""Stub LXD REST server for operator-loop tests and the acceptance suite.

Implements just enough of the LXD REST API surface the executor uses, and
records every request it receives so tests can assert a *real* HTTP call was
made (not just a log line):

  POST   /1.0/instances               -> 202 + an async operation (creates; leaves STOPPED)
  GET    /1.0/operations/{op}         -> metadata.status "Success"
  PUT    /1.0/instances/{name}/state  -> 202 + operation (start/stop), 400 if already running
  GET    /1.0/instances/{name}/state  -> metadata.status "Running"/"Stopped"
  DELETE /1.0/instances/{name}        -> 200 (drops the instance)
  GET    /1.0/images?recursion=1      -> metadata: <configured image list>
  DELETE /1.0/images/{fingerprint}    -> 200
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_INSTANCE_STATE_RE = re.compile(r"^/1\.0/instances/([^/]+)/state$")
_INSTANCE_RE = re.compile(r"^/1\.0/instances/([^/]+)$")


class StubLxd:
    def __init__(self):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.images: list[dict] = []
        # Materialisation surface: instances created by a POST /1.0/instances, and
        # switches to force every create / start to fail (the failure-semantics
        # paths). `instance_state` mirrors LXD's own state machine: a create leaves
        # the instance STOPPED, and only a PUT .../state {"action":"start"} that
        # completes the operation makes it RUNNING.
        self.instances: dict[str, dict] = {}
        self.instance_state: dict[str, str] = {}
        self.fail_create = False
        self.fail_start = False
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args):  # noqa: A002 — silence
                pass

            def _read(self):
                n = int(self.headers.get("Content-Length", 0) or 0)
                if n <= 0:
                    return None
                return json.loads(self.rfile.read(n))

            def _send(self, status: int, obj):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = self._read()
                stub.requests.append(("POST", self.path, body))
                if self.path == "/1.0/instances":
                    name = (body or {}).get("name", "")
                    if stub.fail_create:
                        self._send(500, {"error": "Failed to create instance"})
                        return
                    if name in stub.instances:
                        # LXD's duplicate-name rejection: create_instance maps this
                        # to `already_applied`, not an error.
                        self._send(500, {"error": f'Instance "{name}" already exists'})
                        return
                    stub.instances[name] = body or {}
                    stub.instance_state[name] = "stopped"   # LXD creates STOPPED
                    op = f"/1.0/operations/op-create-{len(stub.instances)}"
                    self._send(202, {"type": "async", "operation": op, "status_code": 100})
                    return
                self._send(200, {"type": "sync", "status_code": 200, "metadata": {}})

            def do_PUT(self):
                body = self._read()
                stub.requests.append(("PUT", self.path, body))
                m = _INSTANCE_STATE_RE.match(self.path)
                name = m.group(1) if m is not None else None
                action = (body or {}).get("action")
                # Only `start` has strict LXD-like semantics (404 unknown, 400 if
                # already running); restart/stop keep the lenient stub behaviour the
                # executor's restart/stop tests rely on.
                if name is not None and action == "start":
                    if name not in stub.instances:
                        self._send(404, {"error": f'Instance "{name}" not found',
                                         "error_code": 404})
                        return
                    if stub.fail_start:
                        self._send(500, {"error": "Failed to start instance"})
                        return
                    if stub.instance_state.get(name) == "running":
                        # Real LXD: starting an already-running instance is an
                        # error; the executor folds it to an idempotent no-op.
                        self._send(400, {"error": f'Instance "{name}" is already running',
                                         "error_code": 400})
                        return
                    stub.instance_state[name] = "running"
                    self._send(202, {"type": "async",
                                     "operation": f"/1.0/operations/op-start-{name}",
                                     "status_code": 100})
                    return
                if name is not None and action == "stop":
                    stub.instance_state[name] = "stopped"
                elif name is not None and action == "restart":
                    stub.instance_state[name] = "running"
                self._send(200, {"type": "async", "operation": "/1.0/operations/op-1",
                                 "status_code": 100})

            def do_DELETE(self):
                stub.requests.append(("DELETE", self.path, None))
                m = _INSTANCE_RE.match(self.path)
                if m is not None:
                    stub.instances.pop(m.group(1), None)
                    stub.instance_state.pop(m.group(1), None)
                self._send(200, {"type": "sync", "status_code": 200, "metadata": {}})

            def do_GET(self):
                stub.requests.append(("GET", self.path, None))
                if self.path.startswith("/1.0/images"):
                    self._send(200, {"type": "sync", "status_code": 200,
                                     "metadata": list(stub.images)})
                elif self.path.startswith("/1.0/operations"):
                    self._send(200, {"type": "sync", "status_code": 200,
                                     "metadata": {"status": "Success", "status_code": 200}})
                else:
                    m = _INSTANCE_STATE_RE.match(self.path)
                    if m is not None:
                        state = stub.instance_state.get(m.group(1), "stopped")
                        status = "Running" if state == "running" else "Stopped"
                        code = 103 if state == "running" else 102
                        self._send(200, {"type": "sync", "status_code": 200,
                                         "metadata": {"status": status, "status_code": code}})
                        return
                    self._send(200, {"type": "sync", "status_code": 200, "metadata": {}})

        return Handler

    def start(self):
        self._thread.start()

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()

    def create_requests(self) -> list[tuple[str, str, dict | None]]:
        """POST /1.0/instances requests (the materialisation calls)."""
        return [r for r in self.requests if r[0] == "POST" and r[1] == "/1.0/instances"]

    def restart_requests(self) -> list[tuple[str, str, dict | None]]:
        """Requests that are a restart of an instance (PUT .../state with action restart)."""
        return [
            (m, p, b) for (m, p, b) in self.requests
            if m == "PUT" and p.endswith("/state") and isinstance(b, dict)
            and b.get("action") == "restart"
        ]

    def start_requests(self) -> list[tuple[str, str, dict | None]]:
        """Requests that start an instance (PUT .../state with action start)."""
        return [
            (m, p, b) for (m, p, b) in self.requests
            if m == "PUT" and p.endswith("/state") and isinstance(b, dict)
            and b.get("action") == "start"
        ]

    def running_instances(self) -> list[str]:
        """Instance names whose state is RUNNING (the materialisation postcondition)."""
        return sorted(n for n, st in self.instance_state.items() if st == "running")
