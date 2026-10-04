"""Stub LXD REST server for operator-loop tests and the acceptance suite.

Implements just enough of the LXD REST API surface the executor uses, and
records every request it receives so tests can assert a *real* HTTP call was
made (not just a log line):

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
                else:
                    self._send(200, {"type": "sync", "status_code": 200, "metadata": {}})

        return Handler

    def start(self):
        self._thread.start()

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()

    def restart_requests(self) -> list[tuple[str, str, dict | None]]:
        """Requests that are a restart of an instance (PUT .../state with action restart)."""
        return [
            (m, p, b) for (m, p, b) in self.requests
            if m == "PUT" and p.endswith("/state") and isinstance(b, dict)
            and b.get("action") == "restart"
        ]
