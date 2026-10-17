"""Stub LXD REST server for operator-loop tests and the acceptance suite.

Implements just enough of the LXD REST API surface the executor uses, and
records every request it receives so tests can assert a *real* HTTP call was
made (not just a log line):

  POST   /1.0/instances                -> 202 + an async operation (creates; STOPPED)
  GET    /1.0/operations/{op}          -> metadata.status (Success by default)
  GET    /1.0/instances/{name}         -> 200 (exists) / 404 (no such instance)
  GET    /1.0/instances/{name}/state   -> metadata.status (the instance state)
  PUT    /1.0/instances/{name}/state   -> 202 + an async operation (state change)
  DELETE /1.0/instances/{name}         -> 200 (drops the instance)
  GET    /1.0/images?recursion=1       -> metadata: <configured image list>
  DELETE /1.0/images/{fingerprint}     -> 200

The single-instance GET is LXD's existence probe, and the executor uses it as its
fail-closed guard before any state change: an instance that `_known()` does not
recognise is answered 404 here exactly as real LXD answers it, so a remediation
aimed at an instance that does not exist is REFUSED rather than attempted. A test
that wants a remediation to land must therefore declare its instance
(`set_state(name, "Running")`, or create it), which is the truth the guard is
enforcing.

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
  * ``strict_stop`` makes a stop of an already-STOPPED instance answer LXD's real
    400 ("is already stopped"), the answer the executor folds to `already_applied`
    (off by default: every other test relies on the lenient stop);
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
        # Teardown surface (#t_28ab1be5): instance names whose DELETE answers a
        # real LXD 500 (device busy) and leaves the instance in place, so a test
        # can prove a per-instance teardown failure is reported and does not abort
        # the rest of the release.
        self.fail_delete: set[str] = set()
        # State-change surface (the single, capitalised state map).
        self.instance_states: dict[str, str] = {}
        self.default_state = "Running"
        self.fail_start = False            # force a start to 500
        self.strict_stop = False           # opt-in: 400 on an already-stopped instance
        self.ignore_actions = False        # accept the action, change nothing
        self.operation_status = "Success"  # status the operation poll reports
        self.operation_error = ""          # the operation's own `err` string (metadata.err)
        self.operation_gone = False        # the op was reaped before we polled it
        # Existence-probe surface: names whose `GET /1.0/instances/<name>` answers
        # 500 (node overloaded / transport-looking failure), so `instance_exists`
        # returns None ("unknown") instead of True/False. Paired with
        # `strict_state_404` to reproduce the 2026-10-17T16:32Z live signature: an
        # unanswerable probe lets the write through, and LXD then answers 404.
        self.probe_unavailable: set[str] = set()
        self.strict_state_404 = False      # PUT .../state on an unknown name -> 404
        # Exec surface (agent task API, t_1a2c913c): the recored stdout/stderr of a
        # `POST /1.0/instances/{name}/exec` with `record-output: true`, served back
        # from `/1.0/instances/{name}/logs/...` exactly like real LXD. `exec_handler`
        # is what the test puts INSIDE the instance: it receives the command argv and
        # returns (returncode, stdout, stderr).
        self.exec_handler = None           # Callable[[list[str]], tuple[int, str, str]]
        self.exec_fail = False             # force the exec POST to 500
        self.log_envelope = False          # serve /logs/<file> wrapped in a sync
                                           # envelope (a proxy might; real LXD does NOT)
        self.exec_operations: dict[str, dict] = {}   # op path -> recorded output
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

        if self.strict_state_404 and not self._known(name):
            # Real LXD's answer to a state change on an instance it does not
            # have: 404 with this body (measured live 2026-10-17T16:32Z).
            return 404, {"type": "error", "status": "", "status_code": 0,
                         "operation": "", "error_code": 404,
                         "error": f'Failed to fetch instance "{name}" in project '
                                  f'"default": Instance not found',
                         "metadata": None}

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

        if action == "stop" and self.strict_stop:
            # Real LXD refuses to stop an already-STOPPED instance; the executor
            # folds this answer to `already_applied` (idempotent no-op). Opt-in so
            # the lenient stub behaviour other tests rely on is unchanged.
            if not self._known(name):
                return 404, {"type": "error", "error_code": 404,
                             "error": f'Instance "{name}" not found'}
            if self.state_of(name) == "Stopped":
                return 400, {"type": "error", "error_code": 400,
                             "error": f'Instance "{name}" is already stopped'}

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

            def _send_text(self, status: int, text: str):
                # How LXD answers `/logs/<file>`: the file's bytes, text/plain.
                body = (text or "").encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = self._read()
                stub.requests.append(("POST", self.path, body))
                if self.path.endswith("/exec") and self.path.startswith("/1.0/instances/"):
                    stub._op_seq += 1
                    op = f"/1.0/operations/op-exec-{stub._op_seq}"
                    if stub.exec_fail:
                        self._send(500, {"error": "Failed to run command"})
                        return
                    argv = (body or {}).get("command") or []
                    rc, out, err = (0, "", "")
                    if stub.exec_handler is not None:
                        rc, out, err = stub.exec_handler(list(argv))
                    name = self.path[len("/1.0/instances/"):-len("/exec")]
                    stdout_path = f"/1.0/instances/{name}/logs/exec-{stub._op_seq}.stdout"
                    stderr_path = f"/1.0/instances/{name}/logs/exec-{stub._op_seq}.stderr"
                    stub.exec_operations[op] = {
                        "return": rc, "stdout": out, "stderr": err,
                        "output": {"1": stdout_path, "2": stderr_path},
                    }
                    stub.requests.append(("EXEC", self.path, body))
                    self._send(202, {
                        "type": "async", "status_code": 100, "operation": op,
                        "metadata": {"description": "Executing command"}})
                    return
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
                    if name in stub.fail_delete:
                        # A real LXD refuses some deletes (device busy); the
                        # instance stays, so the caller must report it.
                        self._send(500, {"type": "error", "error_code": 500,
                                         "error": f"cannot delete instance {name!r}: device busy"})
                        return
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
                    exec_op = stub.exec_operations.get(self.path)
                    if exec_op is not None and stub.operation_status == "Success":
                        # An exec operation reports what the command produced: its
                        # return code and the two recorded output paths (real LXD
                        # shape, see `record-output`).
                        self._send(200, {"type": "sync", "status_code": 200,
                                         "metadata": {
                                             "status": "Success", "status_code": 200,
                                             "err": "",
                                             "metadata": {"return": exec_op["return"],
                                                          "output": exec_op["output"]}}})
                        return
                    self._send(200, {"type": "sync", "status_code": 200,
                                     "metadata": {"status": stub.operation_status,
                                                  "status_code": 200,
                                                  "err": stub.operation_error}})
                elif "/logs/" in self.path:
                    # Recorded exec output, served as LXD serves it: `/logs/<file>`
                    # is a FILE DOWNLOAD — the body is the recorded bytes, plain
                    # text, not a `{"metadata": ...}` object envelope. (The envelope
                    # shape here was a stub fiction; it hid a real defect where every
                    # record-output read came back empty on a live node. Set
                    # `log_envelope = True` to model a proxy that wraps the file.)
                    for op in stub.exec_operations.values():
                        for stream, path in op["output"].items():
                            if path == self.path:
                                text = op["stdout"] if stream == "1" else op["stderr"]
                                if stub.log_envelope:
                                    self._send(200, {"type": "sync", "status_code": 200,
                                                     "metadata": text})
                                else:
                                    self._send_text(200, text)
                                return
                    self._send(404, {"type": "error", "error_code": 404,
                                     "error": "no such log file"})
                elif self.path.startswith("/1.0/instances/") and self.path.endswith("/state"):
                    name = self.path[len("/1.0/instances/"): -len("/state")]
                    state = stub.state_of(name)
                    self._send(200, {"type": "sync", "status_code": 200,
                                     "metadata": {"status": state,
                                                  "status_code": 103 if state == "Running" else 102}})
                elif self.path.startswith("/1.0/instances/") and self.path.count("/") == 3:
                    # LXD's existence probe: GET /1.0/instances/{name}. 404 when the
                    # instance is not there — the answer the executor's fail-closed
                    # guard turns into a refusal instead of a state change.
                    name = self.path[len("/1.0/instances/"):].split("?", 1)[0]
                    if name in stub.probe_unavailable:
                        # The probe could not answer (node overloaded): UNKNOWN,
                        # not "absent". The executor must not read this as a 404.
                        self._send(500, {"type": "error", "error_code": 500,
                                         "error": "Failed to get instance: node overloaded"})
                    elif stub._known(name):
                        self._send(200, {"type": "sync", "status_code": 200,
                                         "metadata": {"name": name,
                                                      "status": stub.state_of(name)}})
                    else:
                        self._send(404, {"type": "error", "error_code": 404,
                                         "error": f'Instance "{name}" not found'})
                elif self.path.startswith("/1.0/instances"):
                    # Existence probe for reconciliation: the names currently
                    # materialised on this node (recursion=1 object list shape).
                    self._send(200, {"type": "sync", "status_code": 200,
                                     "metadata": [{"name": n} for n in stub.instances]})
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

    def delete_requests(self) -> list[tuple[str, str, dict | None]]:
        """DELETE /1.0/instances/{name} requests (the teardown calls)."""
        return [r for r in self.requests if r[0] == "DELETE" and
                r[1].startswith("/1.0/instances/")]

    def exec_requests(self, instance: str | None = None) -> list[tuple[str, str, dict | None]]:
        """Exec calls made inside an instance (the agent task transport)."""
        out = [r for r in self.requests if r[0] == "EXEC"]
        if instance is not None:
            out = [r for r in out if r[1] == f"/1.0/instances/{instance}/exec"]
        return out

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
