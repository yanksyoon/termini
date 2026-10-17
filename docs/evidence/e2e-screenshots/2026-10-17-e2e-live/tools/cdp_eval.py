#!/usr/bin/env python3
"""Minimal Chrome DevTools Protocol client — stdlib only.

Why this exists: the screenshots must show the browser chrome (address bar) so a
reader can see WHICH url produced the pixels, which means a headful Chromium
inside Xvfb rather than a headless page-only grab. Driving that browser needs
CDP, and the capture host has no websocket library installed. So: raw socket,
RFC6455 handshake, one masked text frame out, then read frames until the
response carrying our id arrives (events are skipped, pings are answered).

Usage:
  cdp_eval.py <port> '<js expression>'
  cdp_eval.py <port> @expressions.json      # JSON array of expressions

Prints one JSON object per expression: {"value": ...} | {"error": ...}.
"""
import base64
import json
import os
import socket
import struct
import sys
import urllib.request


def pick_page(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as r:
        targets = json.load(r)
    for t in targets:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return t
    raise SystemExit("no page target with a debugger url")


class WS:
    def __init__(self, url):
        assert url.startswith("ws://")
        hostport, _, path = url[5:].partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=20)
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise SystemExit("handshake closed")
            buf += chunk
        if b"101" not in buf.split(b"\r\n")[0]:
            raise SystemExit("handshake failed: " + buf.split(b"\r\n")[0].decode())
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self._id = 0

    def _recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise SystemExit("socket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _read_frame(self):
        b0, b1 = self._recv_exact(2)
        opcode = b0 & 0x0F
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        return opcode, self._recv_exact(length)

    def _send_text(self, text):
        data = text.encode()
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def call(self, method, params=None, timeout=60):
        self._id += 1
        mid = self._id
        self._send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        self.sock.settimeout(timeout)
        while True:
            opcode, payload = self._read_frame()
            if opcode == 0x9:  # ping -> pong
                self.sock.sendall(b"\x8a\x80" + os.urandom(4))
                continue
            if opcode == 0x8:
                raise SystemExit("server closed the websocket")
            if opcode != 0x1:
                continue
            msg = json.loads(payload.decode())
            if msg.get("id") == mid:
                return msg


def evaluate(ws, expression):
    msg = ws.call(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
    )
    if "error" in msg:
        return {"error": msg["error"]}
    res = msg.get("result", {})
    if res.get("exceptionDetails"):
        return {"exception": res["exceptionDetails"].get("text")}
    return {"value": res.get("result", {}).get("value")}


def main():
    port = sys.argv[1]
    arg = sys.argv[2]
    if arg == "--text":
        ws = WS(pick_page(port)["webSocketDebuggerUrl"])
        res = evaluate(ws, sys.argv[3])
        if "value" in res:
            print(res["value"] if isinstance(res["value"], str) else json.dumps(res["value"]))
        else:
            print(json.dumps(res))
        return
    if arg.startswith("@"):
        with open(arg[1:]) as fh:
            exprs = json.load(fh)
    else:
        exprs = [arg]
    ws = WS(pick_page(port)["webSocketDebuggerUrl"])
    for expr in exprs:
        print(json.dumps(evaluate(ws, expr)))


if __name__ == "__main__":
    main()
