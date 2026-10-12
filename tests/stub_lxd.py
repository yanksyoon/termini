"""Stub LXD REST server for operator-loop tests and the acceptance suite.

Implements just enough of the LXD REST API surface the executor uses, and
records every request it receives so tests can assert a *real* HTTP call was
made (not just a log line):

  POST   /1.0/instances                -> 202 + an async operation (creates; STOPPED)
  GET    /1.0/operations/{op}          -> metadata.status (Success by default)
  GET    /1.0/instances/{name}/state   -> metadata.status (the instance state)
  PUT    /1.0/instances/{name}/state   -> 202 + an async operation (state change)
  DELETE /1.0/instances/{name}         -> 200 (drops the instance)
  GET    /1.0/images?recursion=1       -> metadata: <configured image list>
  DELETE /1.0/images/{fingerprint}     -> 200

State model — ONE capitalised state map, ``instance_states``, shared by the
materialisation tests (#133) and the remediation-effect tests (#136):

  * each instance's state lives in ``instance_states``; ``default_state`` is
    "Running", so an instance never explicitly placed reads Running
    (``state_of``/``set_state`` are the accessors);
  * a create leaves the instance STOPPED (LXD creates but does not start it);
  * a PUT .../state with action start/restart sets it Running, stop sets Stopped
    (the stub applies the action, like a real LXD);
  * ``fail_create`` / ``fail_start`` force a create / a start to 500 (the
    failure-semantics paths);
  * ``start`` keeps LXD's strict semantics: 404 for an unknown instance, 400 for
    one that is already Running (the executor folds that to an idempotent no-op);
  * ``ignore_actions`` models the measured real-LXD no-op where the operation
    reports Success but the instance does not change (e.g. restarting a STOPPED
    container) — the action is accepted and the state is left untouched;
  * ``operation_status`` forces the operation poll to a non-Success status
    ("Failure", or a still-pending "Running" that makes a wait time out);
  * ``operation_gone`` makes the operation poll 404, modelling a finished
    operation LXD reaped before the executor's first poll.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# action -> resulting instance state (the stub applies the action like real LXD)
_ACTION_STATE = {"start": "Running", "restart": "Running", "stop": "Stopped"}


class StubLxd:
    def __init__(self):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.images: list[dict] = []
        # Materialisation surface: instances created by a POST /1.0/instances, and
        # a switch to force every create to fail (the failure-semantics path).
        self.instances: dict[str, dict] = {}
        self.fail_create = False
        # State-change surface (the single, capitalised state map).
        self.instance_states: dict[str, str] = {}
        self.default_state = "Running"
        self.fail_start = False            # force a start to 500
        self.ignore_actions = False        # accept the action, change nothing
        self.operation_status = "Success"  # status the operation poll reports
        self.operation_gone = False        # the op was reaped before we polled it
        self._op_seq = 0
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    # ------------------------------------------------------------- state model
    def state_of(self, name: str) -> str:
        return self.instance_states.get(name, self.default_state)

    def set_state(self, name: str, state: str) -> None:
        self.instance_states[name] = state

    def _known(self, name: str) -> bool:
        """Does this instance exist? Created here, or explicitly given a state.

        A create records the instance in ``instances``; a test that drives a
        pre-existing instance calls ``set_state``. Either makes the instance
        real for the purpose of LXD's 404-on-unknown answer to a start.
        """
        return name in self.instances or name in self.instance_states

    def _apply(self, path: str, body: dict | None):
        """Apply a PUT .../state action, like a real LXD; return (status, body).

        ``start`` carries LXD's strict semantics (404 unknown, 500 when forced to
        fail, 400 if already running) because the executor's start path and its
        idempotency fold depend on them; every other action keeps the lenient
        behaviour the restart/stop tests rely on. A state change is answered
        asynchronously (202 + an operation URL) even when it turns out to be a
        no-op — which is exactly the trap the executor must not fall into.
        """
        name = path[len("/1.0/instances/"): -len("/state")]
        action = (body or {}).get("action", "")

        if action == "start":
            if not self._known(name):
                return 404, {"type": "error", "error_code": 404,
                             "error": f'Instance "{name}" not found'}
            if self.fail_start:
                return 500, {"type": "error", "error_code": 500,
                             "error": "Failed to start instance"}
            if self.state_of(name) == "Running":
                # Real LXD: starting an already-running instance is an error; the
                # executor folds it to an idempotent no-op.
                return 400, {"type": "error", "error_code": 400,
                             "error": f'Instance "{name}" is already running'}
            if not self.ignore_actions:
                self.set_state(name, "Running")
            return 202, {"type": "async", "status_code": 100,
                         "operation": f"/1.0/operations/op-start-{name}",
                         "metadata": {"description": "Starting instance"}}

        if not self.ignore_actions:
            new_state = _ACTION_STATE.get(action)
            if new_state:
                self.set_state(name, new_state)
        self._op_seq += 1
        op = f"/1.0/operations/op-state-{self._op_seq}"
        return 202, {"type": "async", "status_code": 100, "operation": op,
                     "metadata": {"description": f"{action.capitalize()}ing instance"}}

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
                    stub.set_state(name, "Stopped")   # LXD creates STOPPED
                    op = f"/1.0/operations/op-create-{len(stub.instances)}"
                    self._send(202, {"type": "async", "operation": op, "status_code": 100})
                    return
                self._send(200, {"type": "sync", "status_code": 200, "metadata": {}})

            def do_PUT(self):
                body = self._read()
                stub.requests.append(("PUT", self.path, body))
                if self.path.startswith("/1.0/instances/") and self.path.endswith("/state"):
                    status, resp = stub._apply(self.path, body)
                    self._send(status, resp)
                    return
                self._send(200, {"type": "sync", "status_code": 200, "metadata": {}})

            def do_DELETE(self):
                stub.requests.append(("DELETE", self.path, None))
                if self.path.startswith("/1.0/instances/"):
                    name = self.path.rsplit("/", 1)[-1]
                    stub.instances.pop(name, None)
                    stub.instance_states.pop(name, None)
                self._send(200, {"type": "sync", "status_code": 200, "metadata": {}})

            def do_GET(self):
                stub.requests.append(("GET", self.path, None))
                if self.path.startswith("/1.0/images"):
                    self._send(200, {"type": "sync", "status_code": 200,
                                     "metadata": list(stub.images)})
                elif self.path.startswith("/1.0/operations/"):
                    if stub.operation_gone:
                        # a finished operation LXD already reaped: 404, like real LXD
                        self._send(404, {"type": "error", "error_code": 404,
                                         "error": "not found"})
                        return
                    self._send(200, {"type": "sync", "status_code": 200,
                                     "metadata": {"status": stub.operation_status,
                                                  "status_code": 200}})
                elif self.path.startswith("/1.0/instances/") and self.path.endswith("/state"):
                    name = self.path[len("/1.0/instances/"): -len("/state")]
                    state = stub.state_of(name)
                    self._send(200, {"type": "sync", "status_code": 200,
                                     "metadata": {"status": state,
                                                  "status_code": 103 if state == "Running" else 102}})
                else:
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

    def state_requests(self, action: str | None = None) -> list[tuple[str, str, dict | None]]:
        """PUT .../state requests, optionally filtered by action.

        ``state_requests()`` is every state change (start/restart/stop);
        ``state_requests("start")`` is only the ones that started an instance.
        """
        out = [
            (m, p, b) for (m, p, b) in self.requests
            if m == "PUT" and p.endswith("/state") and isinstance(b, dict)
        ]
        if action is not None:
            out = [r for r in out if r[2].get("action") == action]
        return out

    def running_instances(self) -> list[str]:
        """Instance names whose state is RUNNING (the materialisation postcondition)."""
        return sorted(n for n, st in self.instance_states.items() if st == "Running")
