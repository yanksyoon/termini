#!/usr/bin/env python3
"""Prove the dashboard's live-stream handshakes, over ws:// **and** wss://.

Why this exists
---------------
The live log panel was reported broken because a probe of the deployed
WebSocket returned HTTP 400. That 400 was **not** the controller and **not** the
proxy rejecting the upgrade: the probe client sent a *cleartext* HTTP upgrade
into the controller's TLS listener, and Caddy (Go ``net/http``) answers
cleartext-on-an-HTTPS-port with

    HTTP/1.0 400 Bad Request

which is indistinguishable in a one-line failure message from a genuinely
rejected WebSocket handshake. This script makes the difference visible: it
prints the raw status line, and ``--reproduce-cleartext-400`` reproduces that
400 on purpose next to the passing TLS handshake.

What it probes
--------------
  * ``/stream/v1?token=…``        — the unified event stream (WS mirror)
  * ``/v1/projects/{id}/logs``    — the SSE endpoint the dashboard panel uses

Usage
-----
    # local demo (plaintext)
    python3 w7/acceptance/probe_stream_ws.py \\
        --url "ws://127.0.0.1:8085/stream/v1?token=demo-project-token"

    # deployed TLS controller — the judge's path
    python3 w7/acceptance/probe_stream_ws.py \\
        --url "wss://34-93-190-64.sslip.io/stream/v1?token=demo-project-token" \\
        --sse --project 01J0DEM0WARM000000000000AB

    # show the misleading failure the old client produced
    python3 w7/acceptance/probe_stream_ws.py \\
        --url "wss://34-93-190-64.sslip.io/stream/v1?token=demo-project-token" \\
        --reproduce-cleartext-400

Self-signed fronts: pass ``--insecure`` (or set ``TERMINI_WS_INSECURE=1``) or
``--ca-file <path>``. The deployed controller serves a real Let's Encrypt
certificate, so neither is needed there.

Exit code is 0 iff every requested handshake succeeded.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import ssl
import sys
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lib.ws import WsClient  # noqa: E402

# The verbatim answer Caddy/Go gives when cleartext HTTP reaches a TLS listener.
GO_CLEARTEXT_400 = b"HTTP/1.0 400 Bad Request\r\n\r\nClient sent an HTTP request to an HTTPS server."


def probe_ws(url: str, frames: int, verify: bool, ca_file: str | None) -> bool:
    print(f"--- WebSocket {url.split('?')[0]} ({urlsplit(url).scheme}) ---")
    try:
        ws = WsClient(url, timeout=12, verify=verify, ca_file=ca_file)
        ws.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"    FAILED: {type(exc).__name__}: {exc}")
        return False
    print("    101 Switching Protocols (upgrade accepted)")
    got = 0
    try:
        for _ in range(frames):
            frame = ws.recv_frame(timeout=12)
            if frame is None:
                break
            got += 1
            data = frame.get("data", "")
            try:
                obj = json.loads(data)
                event = obj.get("event", "?")
                detail = str(obj.get("data", ""))[:110]
            except (ValueError, AttributeError):
                event, detail = "?", str(data)[:110]
            print(f"    frame {got}: event={event} data={detail}")
    except TimeoutError:
        print("    (no frame within the timeout — the handshake still succeeded)")
    finally:
        ws.close()
    print(f"    frames received: {got}")
    return True


def probe_sse(controller_root: str, project: str, token: str, seconds: float, verify: bool,
              ca_file: str | None) -> bool:
    """EventSource handshake the dashboard panel makes: a plain GET that streams."""
    # Normalise a ws/wss controller URL to its http/https equivalent — the SSE
    # probe must do TLS for an https front, exactly like wss:// does for the WS.
    u = urlsplit(controller_root)
    secure = u.scheme in ("https", "wss")
    scheme = "https" if secure else "http"
    host = u.hostname
    port = u.port or (443 if secure else 80)
    endpoint = f"{scheme}://{host}:{port}"
    print(f"--- SSE {endpoint}/v1/projects/{project}/logs ---")
    try:
        raw = socket.create_connection((host, port), timeout=10)
        if secure:
            ctx = ssl.create_default_context(cafile=ca_file)
            if not verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(raw, server_hostname=host)
        else:
            sock = raw
    except Exception as exc:  # noqa: BLE001
        print(f"    FAILED: {type(exc).__name__}: {exc}")
        return False
    path = f"/v1/projects/{project}/logs?token={token}"
    sock.sendall(
        f"GET {path} HTTP/1.1\r\nHost: {host}\r\nAccept: text/event-stream\r\n"
        "Cache-Control: no-cache\r\n\r\n".encode()
    )
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            print("    FAILED: connection closed before headers")
            return False
        buf += chunk
    head, _, body = buf.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
    print(f"    {status_line}")
    print(f"    Content-Type: {'text/event-stream' if b'text/event-stream' in head else 'MISSING'}")
    ok = "200" in status_line and b"text/event-stream" in head
    sock.settimeout(seconds)
    try:
        while True:
            more = sock.recv(4096)
            if not more:
                break
            body += more
    except (socket.timeout, TimeoutError):
        pass
    finally:
        sock.close()
    events = [e for e in body.decode(errors="replace").split("\n\n") if e.strip()]
    print(f"    events seen in {seconds:.0f}s: {len(events)}")
    if events:
        print(f"    first: {events[0][:200]!r}")
    return ok


def reproduce_cleartext_400(url: str) -> bool:
    """Send the plaintext upgrade into the TLS port — the old client's behaviour.

    Reproduces the misleading 400 exactly, so the diagnosis is checkable rather
    than asserted. Succeeds (returns True) when the 400 is observed.
    """
    u = urlsplit(url)
    host = u.hostname
    port = u.port or (443 if u.scheme == "wss" else 80)
    print(f"--- control: plaintext upgrade bytes written to {host}:{port} (TLS listener) ---")
    try:
        sock = socket.create_connection((host, port), timeout=10)
    except Exception as exc:  # noqa: BLE001
        print(f"    FAILED to connect: {type(exc).__name__}: {exc}")
        return False
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n".encode()
    )
    buf = b""
    try:
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()
    status_line = buf.split(b"\r\n", 1)[0].decode(errors="replace") if buf else "(no response)"
    print(f"    -> {status_line}")
    observed = "400" in status_line
    print(f"    observed the misdiagnosed 400: {observed}")
    return observed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="full ws:// or wss:// URL incl. ?token=")
    ap.add_argument("--frames", type=int, default=3, help="frames to read after the upgrade (default 3)")
    ap.add_argument("--project", default=os.environ.get("TERMINI_PROJECT_ID", ""),
                    help="project ULID, for --sse")
    ap.add_argument("--sse", action="store_true",
                    help="also probe the SSE log endpoint the dashboard panel uses")
    ap.add_argument("--sse-seconds", type=float, default=8.0)
    ap.add_argument("--ca-file", default=None, help="CA bundle for a self-signed front")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed only)")
    ap.add_argument("--reproduce-cleartext-400", action="store_true",
                    help="also reproduce the misleading HTTP/1.0 400 the old no-TLS probe produced")
    args = ap.parse_args(argv)

    verify = not args.insecure
    print(f"### Termini stream handshake probe\n")
    ok = probe_ws(args.url, args.frames, verify, args.ca_file)
    if args.sse:
        base = args.url.split("?")[0]
        # /stream/v1 -> controller root
        root = base[: base.index("/stream/v1")] if "/stream/v1" in base else base
        ok = probe_sse(root, args.project, _token(args.url), args.sse_seconds, verify, args.ca_file) and ok
    if args.reproduce_cleartext_400:
        # Control: this is expected to reproduce the 400; it does not affect ok.
        reproduce_cleartext_400(args.url)
    print(f"\n### result: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def _token(url: str) -> str:
    for part in urlsplit(url).query.split("&"):
        if part.startswith("token="):
            return part[len("token="):]
    return os.environ.get("TERMINI_PROJECT_TOKEN", "")


if __name__ == "__main__":
    raise SystemExit(main())
