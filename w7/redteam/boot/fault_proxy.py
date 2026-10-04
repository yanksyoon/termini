#!/usr/bin/env python3
"""fault_proxy.py — a deliberately-degradable TCP/HTTP proxy in front of the
controller used for the Termini boot-latency red-team.

It models the boot-time network degradations the W7 red-team must exercise
without touching the real demo controller:

  mode=delay    accept the connection, then sleep DELAY_S before forwarding
                (a delayed / slow registration or "metadata" service)
  mode=refuse   accept then immediately close (connection reset)
  mode=stall    accept, never respond (silent black hole)

Usage:
  DELAY_S=5 python3 fault_proxy.py --listen-port 8098 --upstream-port 8099
"""
from __future__ import annotations

import argparse
import http.server
import os
import socket
import socketserver
import sys
import time
import urllib.error
import urllib.request

MODE = os.environ.get("FAULT_MODE", "delay")
DELAY_S = float(os.environ.get("DELAY_S", "0"))
UPSTREAM = ("127.0.0.1", 8099)
LOG = sys.stdout


def log(msg: str) -> None:
    LOG.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} [fault_proxy mode={MODE} delay={DELAY_S}] {msg}\n")
    LOG.flush()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _fault(self) -> bool:
        if MODE == "refuse":
            self.connection.close()
            log("refused connection")
            return True
        if MODE == "stall":
            log("stalling connection (no response)")
            time.sleep(3600)
            return True
        if MODE == "delay" and DELAY_S > 0:
            time.sleep(DELAY_S)
        return False

    def _proxy(self) -> None:
        if self._fault():
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else None
        target = f"http://127.0.0.1:{UPSTREAM[1]}{self.path}"
        req = urllib.request.Request(target, data=body, method=self.command)
        for k, v in self.headers.items():
            if k.lower() in ("host", "content-length", "connection"):
                continue
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                payload = r.read()
                self.send_response(r.status)
                for k, v in r.headers.items():
                    if k.lower() in ("transfer-encoding", "connection", "content-length"):
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as e:
            payload = e.read()
            self.send_response(e.code)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as e:  # noqa: BLE001
            log(f"upstream error: {e!r}")
            try:
                self.send_response(502)
                self.end_headers()
            except Exception:  # noqa: BLE001
                pass

    do_GET = _proxy
    do_POST = _proxy

    def log_message(self, *args) -> None:  # silence default logging
        return


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    global UPSTREAM
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen-port", type=int, default=8098)
    ap.add_argument("--upstream-port", type=int, default=8099)
    args = ap.parse_args()
    UPSTREAM = ("127.0.0.1", args.upstream_port)
    log(f"listening on 0.0.0.0:{args.listen_port} -> 127.0.0.1:{args.upstream_port}")
    with Server(("0.0.0.0", args.listen_port), Handler) as httpd:
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
