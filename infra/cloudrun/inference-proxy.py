#!/usr/bin/env python3
"""Add a fresh OIDC identity token to Cloud Run inference requests.

Why this exists
---------------
The Gemma service is deployed authenticated (`--no-allow-unauthenticated`), so
callers need an OIDC identity token. Termini's decision adapter
(`termini/decision/config.py`) takes a *static* bearer string from
`TERMINI_INFERENCE_TOKEN`, and Cloud Run identity tokens expire after ~1 hour —
so pasting one into an env var produces a service that works, then mysteriously
stops.

This proxy sits on loopback in front of the controller and mints a fresh token
per request from the GCE metadata server. The controller then points at
`http://127.0.0.1:8090` with no static secret, and the model service stays
private. No change to the controller or adapter is needed.
"""
from __future__ import annotations

import http.server
import json
import socketserver
import urllib.error
import urllib.request

UPSTREAM = "https://termini-gemma-tylgzp65ma-as.a.run.app"
LISTEN = ("127.0.0.1", 8090)
METADATA = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/identity?audience=" + UPSTREAM
)


def identity_token() -> str:
    req = urllib.request.Request(METADATA, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode().strip()


class Proxy(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _forward(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(UPSTREAM + self.path, data=body, method=method)
        try:
            req.add_header("Authorization", "Bearer " + identity_token())
        except Exception as e:  # metadata unavailable — fail closed, loudly
            self._reply(502, json.dumps({"error": f"metadata: {e}"}).encode())
            return
        req.add_header("Content-Type", self.headers.get("Content-Type", "application/json"))
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                self._reply(r.status, r.read())
        except urllib.error.HTTPError as e:
            self._reply(e.code, e.read())
        except Exception as e:
            self._reply(502, json.dumps({"error": str(e)}).encode())

    def _reply(self, code: int, data: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._forward("GET")

    def do_POST(self) -> None:
        self._forward("POST")

    def log_message(self, format, *args) -> None:  # quiet
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server(LISTEN, Proxy) as srv:
        srv.serve_forever()
