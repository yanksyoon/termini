"""Stub LXD REST server for operator-loop tests and the acceptance suite.

Implements just enough of the LXD REST API surface the executor uses, and
records every request it receives so tests can assert a *real* HTTP call was
made (not just a log line):

  POST   /1.0/instances               -> 202 + an async operation (creates)
  GET    /1.0/operations/{op}         -> metadata.status "Success"
  PUT    /1.0/instances/{name}/state  -> 200 (records action/force)
  DELETE /1.0/instances/{name}        -> 200
  GET    /1.0/images?recursion=1      -> metadata: <configured image list>
  DELETE /1.0/images/{fingerprint}    -> 200
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class StubLxd:
    def __init__(self):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.images: list[dict] = []
        # Materialisation surface: instances created by a POST /1.0/instances, and
        # a switch to force every create to fail (the failure-semantics path).
        self.instances: dict[str, dict] = {}
        self.fail_create = False
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
                    op = f"/1.0/operations/op-create-{len(stub.instances)}"
                    self._send(202, {"type": "async", "operation": op, "status_code": 100})
                    return
                self._send(200, {"type": "sync", "status_code": 200, "metadata": {}})

            def do_PUT(self):
                body = self._read()
                stub.requests.append(("PUT", self.path, body))
                self._send(200, {"type": "async", "operation": "/1.0/operations/op-1",
                                 "status_code": 100})

            def do_DELETE(self):
                stub.requests.append(("DELETE", self.path, None))
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
