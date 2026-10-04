#!/usr/bin/env python3
"""Dev mock of the Termini terminal bridge — stdlib only, zero dependencies.

This stands in for the *real* controller endpoints owned by task t_3468e5f0 so
the web-terminal UX can be built and tested before that backend lands. It is a
faithful implementation of the frozen wire contract in
``docs/contracts/dashboard.md`` §3:

  * ``GET /terminal/v1/nodes/{node_id}?token=<project_token>`` — WebSocket
    terminal (base64 PTY relay, input/resize/ping framing, ready/output/exit/error).
  * ``GET /v1/projects/{pid}/nodes`` — node list for the selector.
  * ``GET /v1/projects/{pid}/nodes/{nid}/ssh`` — SSH access-string payload.

Auth: an invalid/missing ``token`` is rejected with WS close code 4401
``unauthorized`` before any frame is exchanged. A node marked ``offline``
returns ``{"type":"error","code":"NODE_OFFLINE",...}`` then closes, so the
frontend's unreachable-node path is exercisable.

It also serves the static UI from ``frontend/terminal/`` so a browser can load
the demo with a single command::

    python3 frontend/server/terminal_bridge.py          # http://127.0.0.1:8091

This is a *dev mock*: it shells out to a local ``bash`` on the machine running
it. Do not deploy it. The production bridge authenticates per-node and proxies
to the node host.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import json
import mimetypes
import os
import posixpath
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------- #
# Frozen protocol constants
# --------------------------------------------------------------------------- #
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA
CLOSE_UNAUTHORIZED = 4401

HERE = os.path.dirname(os.path.abspath(__file__))
DOCROOT = os.path.normpath(os.path.join(HERE, "..", "terminal"))

DEFAULT_NODES = [
    {
        "node_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C1E",
        "role": "compute",
        "state": "running",
        "public_ip": "203.0.113.10",
        "offline": False,
    },
    {
        "node_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C2F",
        "role": "inference",
        "state": "running",
        "public_ip": "203.0.113.11",
        "offline": False,
    },
    {
        "node_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C3G",
        "role": "compute",
        "state": "offline",
        "public_ip": "203.0.113.12",
        "offline": True,
    },
]


# --------------------------------------------------------------------------- #
# Minimal RFC 6455 WebSocket framing (server side)
# --------------------------------------------------------------------------- #
class WSConn:
    """Server-side WebSocket over an already-upgraded socket file pair."""

    def __init__(self, rfile, wfile):
        self.rfile = rfile
        self.wfile = wfile
        self._lock = threading.Lock()
        self.closed = False

    # ----- handshake -----
    @staticmethod
    def accept_key(sec_key: str) -> str:
        import hashlib

        digest = hashlib.sha1((sec_key + WS_GUID).encode("ascii")).digest()
        return base64.b64encode(digest).decode("ascii")

    def handshake(self, sec_key: str) -> None:
        resp = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {self.accept_key(sec_key)}\r\n"
            "\r\n"
        )
        self.wfile.write(resp.encode("ascii"))
        self.wfile.flush()

    # ----- sending -----
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray()
        header.append(0x80 | opcode)
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header.extend(struct.pack("!H", n))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", n))
        with self._lock:
            if self.closed:
                return
            self.wfile.write(bytes(header) + payload)
            self.wfile.flush()

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def send_json(self, obj: dict) -> None:
        self.send_text(json.dumps(obj, separators=(",", ":")))

    def send_pong(self, payload: bytes = b"") -> None:
        self._send_frame(OP_PONG, payload)

    def send_close(self, code: int = 1000, reason: str = "") -> None:
        body = struct.pack("!H", code) + reason.encode("utf-8")
        try:
            self._send_frame(OP_CLOSE, body)
        finally:
            self.closed = True

    # ----- receiving -----
    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.rfile.read(n - len(buf))
            if not chunk:
                raise EOFError("socket closed")
            buf.extend(chunk)
        return bytes(buf)

    def read_frame(self):
        """Return (opcode, payload) or raise EOFError when the peer is gone."""
        b0 = self._read_exact(1)[0]
        opcode = b0 & 0x0F
        b1 = self._read_exact(1)[0]
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else b"\x00\x00\x00\x00"
        payload = bytearray(self._read_exact(length)) if length else bytearray()
        if masked:
            for i in range(length):
                payload[i] ^= mask[i % 4]
        return opcode, bytes(payload)


# --------------------------------------------------------------------------- #
# PTY-backed terminal session
# --------------------------------------------------------------------------- #
class TerminalSession:
    """Relays one interactive shell <-> one WebSocket client."""

    def __init__(self, node: dict, conn: WSConn, shell: str, registry: dict):
        self.node = node
        self.conn = conn
        self.shell = shell
        self.registry = registry
        self.node_id = node["node_id"]
        self._stop = threading.Event()
        self._reader = None
        self.master = None
        self.proc = None
        self._user = os.environ.get("USER") or "ubuntu"

    def run(self) -> None:
        # Session lifecycle: one session per node — replace any existing one.
        old = self.registry.get(self.node_id)
        if old is not None and old is not self:
            try:
                old.conn.send_json(
                    {"type": "error", "code": "SESSION_REPLACED", "message": "session replaced"}
                )
                old.conn.send_close(1000, "session replaced")
            except Exception:
                pass
            old.stop()
        self.registry[self.node_id] = self

        try:
            self._spawn()
            self.conn.send_json(
                {
                    "type": "ready",
                    "node_id": self.node_id,
                    "user": self._user,
                    "shell": self.shell,
                }
            )
            self._reader = threading.Thread(target=self._pump_output, daemon=True)
            self._reader.start()
            self._pump_input()
        finally:
            self.stop()
            self._cleanup()
            if self.registry.get(self.node_id) is self:
                self.registry.pop(self.node_id, None)

    # ----- process -----
    def _spawn(self) -> None:
        master, slave = pty.openpty()
        self.master = master
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        env.setdefault("PS1", r"\u@termini-demo:\w\$ ")
        env["HISTFILE"] = "/dev/null"
        self.proc = subprocess.Popen(
            [self.shell, "-i"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            preexec_fn=os.setsid,
            close_fds=True,
        )
        os.close(slave)
        self._resize(80, 24)

    def _resize(self, cols: int, rows: int) -> None:
        if self.master is None:
            return
        winsize = struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0)
        try:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    def stop(self) -> None:
        self._stop.set()

    def _cleanup(self) -> None:
        if self.proc is not None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGHUP)
            except ProcessLookupError:
                pass
            except Exception:
                pass
            try:
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = None

    # ----- relay -----
    def _pump_output(self) -> None:
        master = self.master
        proc = self.proc
        if master is None:
            return
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([master], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not r:
                if proc is not None and proc.poll() is not None:
                    break
                continue
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            try:
                self.conn.send_json(
                    {"type": "output", "data": base64.b64encode(data).decode("ascii")}
                )
            except Exception:
                break
        if not self._stop.is_set():
            code = self.proc.poll() if self.proc else 0
            try:
                self.conn.send_json(
                    {"type": "exit", "code": int(code or 0), "reason": "normal"}
                )
                self.conn.send_close(1000, "shell exited")
            except Exception:
                pass
            self._stop.set()

    def _pump_input(self) -> None:
        while not self._stop.is_set():
            try:
                opcode, payload = self.conn.read_frame()
            except (EOFError, OSError):
                break
            if opcode == OP_CLOSE:
                break
            if opcode == OP_PING:
                self.conn.send_pong(payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode not in (OP_TEXT, OP_CONT):
                continue
            try:
                msg = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            mtype = msg.get("type")
            if mtype == "input":
                try:
                    data = base64.b64decode(msg.get("data", ""), validate=False)
                except Exception:
                    continue
                if self.master is not None:
                    try:
                        os.write(self.master, data)
                    except OSError:
                        break
            elif mtype == "resize":
                self._resize(int(msg.get("cols", 80)), int(msg.get("rows", 24)))
            elif mtype == "ping":
                self.conn.send_json({"type": "pong"})
        self._stop.set()


# --------------------------------------------------------------------------- #
# HTTP + WS router
# --------------------------------------------------------------------------- #
class BridgeHandler(BaseHTTPRequestHandler):
    server: "BridgeServer"  # type: ignore[assignment]  # set by socketserver
    server_version = "termini-mock-bridge/0.1"
    protocol_version = "HTTP/1.1"

    # config (nodes/token/shell/registry) lives on the BridgeServer instance
    # and is reached via self.server.

    def log_message(self, format, *args):  # noqa: A002 - http.server API name
        sys.stderr.write("[mock-bridge] %s - %s\n" % (self.address_string(), format % args))

    # ----- helpers -----
    def _json(self, status: int, obj) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _node(self, node_id: str):
        for n in self.server.nodes:
            if n["node_id"] == node_id:
                return n
        return None

    def _serve_static(self, path: str) -> None:
        rel = posixpath.normpath(urllib.parse.unquote(path)).lstrip("/")
        if rel in ("", "."):
            rel = "index.html"
        target = os.path.normpath(os.path.join(DOCROOT, rel))
        if not target.startswith(DOCROOT + os.sep) and target != DOCROOT:
            self.send_error(404, "not found")
            return
        if not os.path.isfile(target):
            self.send_error(404, "not found")
            return
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if target.endswith(".js"):
            ctype = "text/javascript"
        elif target.endswith(".css"):
            ctype = "text/css"
        elif target.endswith(".html"):
            ctype = "text/html; charset=utf-8"
        with open(target, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ----- GET -----
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        segs = [s for s in path.split("/") if s]

        # WebSocket terminal
        if path.startswith("/terminal/v1/"):
            return self._handle_terminal(segs, parsed)

        # SSH access payload
        if len(segs) == 6 and segs[0] == "v1" and segs[1] == "projects" and segs[3] == "nodes" and segs[5] == "ssh":
            node = self._node(segs[4])
            if node is None:
                return self._json(404, {"error": {"code": "NOT_FOUND", "message": "no such node"}})
            return self._json(200, self._ssh_payload(node))

        # Node list
        if len(segs) == 4 and segs[0] == "v1" and segs[1] == "projects" and segs[3] == "nodes":
            return self._json(200, {"nodes": [self._node_public(n) for n in self.server.nodes]})

        return self._serve_static(path)

    def _node_public(self, n: dict) -> dict:
        return {
            "node_id": n["node_id"],
            "role": n["role"],
            "state": n["state"],
            "last_seen_at": None if n["offline"] else "2026-10-04T09:00:00Z",
        }

    def _ssh_payload(self, n: dict) -> dict:
        return {
            "node_id": n["node_id"],
            "user": "ubuntu",
            "host": n["public_ip"],
            "port": 22,
            "key_algorithm": "ed25519",
            "key_fingerprint": "SHA256:AbC123dEf456GhI789jKl012MnO345pQr678StU==",
            "key_comment": "termini-breakglass",
            # server-computed hint; the client re-derives and sanitizes anyway
            "command": f"ssh -i ~/.ssh/termini_breakglass ubuntu@{n['public_ip']}",
        }

    # ----- WebSocket terminal -----
    def _handle_terminal(self, segs, parsed) -> None:
        # /terminal/v1/nodes/{node_id}
        node_id = segs[3] if len(segs) >= 4 and segs[2] == "nodes" else ""
        upgrade = (self.headers.get("Upgrade") or "").lower()
        sec_key = self.headers.get("Sec-WebSocket-Key")
        if upgrade != "websocket" or not sec_key:
            return self._json(400, {"error": {"code": "INVALID_ARGUMENT", "message": "expected websocket upgrade"}})

        conn = WSConn(self.rfile, self.wfile)
        conn.handshake(sec_key)
        self.close_connection = True

        token = dict(urllib.parse.parse_qsl(parsed.query)).get("token")
        if token != self.server.token:
            conn.send_close(CLOSE_UNAUTHORIZED, "unauthorized")
            return

        node = self._node(node_id)
        if node is None:
            conn.send_json({"type": "error", "code": "NOT_FOUND", "message": "no such node"})
            conn.send_close(1000, "not found")
            return
        if node["offline"]:
            conn.send_json({"type": "error", "code": "NODE_OFFLINE", "message": "node not reachable"})
            conn.send_close(1011, "node offline")
            return

        TerminalSession(node, conn, self.server.shell, self.server.registry).run()


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    # Injected by build_server().
    nodes: list
    token: str
    shell: str
    registry: dict


def build_server(host: str, port: int, token: str, shell: str, nodes=None) -> BridgeServer:
    srv = BridgeServer((host, port), BridgeHandler)
    srv.nodes = list(nodes if nodes is not None else DEFAULT_NODES)
    srv.token = token
    srv.shell = shell
    srv.registry = {}
    return srv


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Termini dev mock terminal bridge")
    ap.add_argument("--host", default=os.environ.get("TERMINI_BRIDGE_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("TERMINI_BRIDGE_PORT", "8091")))
    ap.add_argument("--token", default=os.environ.get("TERMINI_PROJECT_TOKEN", "demo-project-token"))
    ap.add_argument("--shell", default=os.environ.get("SHELL", "/bin/bash"))
    args = ap.parse_args(argv)

    srv = build_server(args.host, args.port, args.token, args.shell)
    url = f"http://{args.host}:{srv.server_address[1]}/?api=http://{args.host}:{srv.server_address[1]}&token={args.token}"
    print(f"[mock-bridge] serving frontend/terminal on {url}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
