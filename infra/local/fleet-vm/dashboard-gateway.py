#!/usr/bin/env python3
"""dashboard-gateway.py — one local origin for the Termini dashboard + controller API.

The fleet now runs as LXD VMs inside this host, so the controller listens on the
LXD bridge (10.14.100.0/24), which a browser on the tailnet cannot route to. This
gateway publishes ONE origin on the host:

    GET  /            -> the static dashboard (dashboard/ from the repo)
    POST /v1/...      -> proxied verbatim to the controller
    GET  /v1/...      -> proxied verbatim (including the SSE log stream)
    GET  /stream/v1   -> proxied verbatim (websocket upgrade is not proxied)

Same-origin is the point: the dashboard's `apiBase` becomes `/v1`, so there is no
CORS preflight and no second port to punch through the tailnet. The project token
is still pasted into the dashboard's Settings panel by the operator; this gateway
never stores or invents a credential.

Usage:
    python3 dashboard-gateway.py --controller http://10.14.100.10:8080 \
        --dashboard /srv/termini/dashboard --port 8099 [--bind 0.0.0.0]
"""
from __future__ import annotations

import argparse
import http.client
import os
import sys
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PROXY_PREFIXES = ("/v1/", "/stream/", "/install/", "/terminal/")


class Gateway(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    controller: tuple = ("10.14.100.10", 8080)
    quiet = True

    # ---- logging ----------------------------------------------------------- #
    def log_message(self, format, *args):  # noqa: A002,A003 - stdlib signature
        if not self.quiet:
            sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def _is_proxy(self) -> bool:
        return any(self.path.startswith(p) for p in PROXY_PREFIXES)

    # ---- proxy ------------------------------------------------------------- #
    def _proxy(self, method: str, body: bytes | None = None) -> None:
        host, port = self.controller
        url = urllib.parse.urlsplit(self.path)
        path = url.path + (("?" + url.query) if url.query else "")
        headers = {}
        for k, v in self.headers.items():
            if k.lower() in ("host", "connection", "content-length", "accept-encoding"):
                continue
            headers[k] = v
        if body is not None:
            headers["Content-Length"] = str(len(body))
        conn = http.client.HTTPConnection(host, port, timeout=300)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
        except Exception as exc:  # noqa: BLE001 - report, never hang
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.log_message("controller unreachable: %s", exc)
            conn.close()
            return

        ctype = resp.getheader("Content-Type", "application/octet-stream")
        # SSE and chunked responses are streamed as they arrive.
        streaming = ctype.startswith("text/event-stream") or resp.getheader("Transfer-Encoding")
        self.send_response(resp.status)
        self.send_header("Content-Type", ctype)
        for h in ("Cache-Control", "X-Accel-Buffering"):
            v = resp.getheader(h)
            if v:
                self.send_header(h, v)
        if streaming:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            payload = resp.read()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        conn.close()

    # ---- verbs ------------------------------------------------------------- #
    def do_GET(self):  # noqa: N802 - stdlib signature
        if self._is_proxy():
            return self._proxy("GET")
        return super().do_GET()

    def do_HEAD(self):  # noqa: N802
        if self._is_proxy():
            return self._proxy("HEAD")
        return super().do_HEAD()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self._is_proxy():
            return self._proxy("POST", body)
        self.send_error(404, "not a gateway route")

    def do_DELETE(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self._is_proxy():
            return self._proxy("DELETE", body)
        self.send_error(404, "not a gateway route")

    def do_PUT(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self._is_proxy():
            return self._proxy("PUT", body)
        self.send_error(404, "not a gateway route")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--controller", default="http://10.14.100.10:8080")
    ap.add_argument("--dashboard", default="dashboard")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    u = urllib.parse.urlsplit(args.controller)
    Gateway.controller = (u.hostname, u.port or 80)
    Gateway.quiet = not args.verbose

    dashboard = os.path.abspath(args.dashboard)
    if not os.path.isdir(dashboard):
        print(f"dashboard directory not found: {dashboard}", file=sys.stderr)
        return 2
    os.chdir(dashboard)

    srv = ThreadingHTTPServer((args.bind, args.port), Gateway)
    srv.daemon_threads = True
    print(f"dashboard gateway: http://{args.bind}:{args.port}/  -> api {args.controller}, "
          f"static {dashboard}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
