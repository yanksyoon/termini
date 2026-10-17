#!/usr/bin/env python3
"""Minimal CDP driver for the HIG variant audit (stdlib only).

The capture host has no websocket library (and no browser automation package),
so this is a raw RFC6455 client plus the few CDP domains the audit needs:
Page (navigate/capture), Runtime (evaluate), Emulation (viewport), Input (keys).

    cdp.py eval '<js expression>'          -> JSON value
    cdp.py shot <path> [--full]            -> PNG at <path>
    cdp.py load <url> [--wait ms]
    cdp.py size <w> <h> [--mobile]
    cdp.py key <Key> [--shift] [--code X]
    cdp.py media <light|dark>              -> emulate prefers-color-scheme
    cdp.py url                             -> location.href

The browser is expected on 127.0.0.1:9222 (see start.sh).
"""
import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request

PORT = int(os.environ.get("CDP_PORT", "9222"))


class WS:
    def __init__(self, url):
        assert url.startswith("ws://")
        hostport, _, path = url[5:].partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
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
            chunk = self.sock.recv(1 << 20)
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

    def call(self, method, params=None, timeout=120):
        self._id += 1
        mid = self._id
        self._send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        self.sock.settimeout(timeout)
        while True:
            opcode, payload = self._read_frame()
            if opcode == 0x9:
                self.sock.sendall(b"\x8a\x80" + os.urandom(4))
                continue
            if opcode == 0x8:
                raise SystemExit("server closed the websocket")
            if opcode != 0x1:
                continue
            msg = json.loads(payload.decode())
            if msg.get("id") == mid:
                return msg


def page_target():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=10) as r:
        targets = json.load(r)
    for t in targets:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return t
    raise SystemExit("no page target")


def connect():
    ws = WS(page_target()["webSocketDebuggerUrl"])
    ws.call("Page.enable")
    ws.call("Runtime.enable")
    return ws


def evaluate(ws, expression):
    msg = ws.call(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
    )
    if "error" in msg:
        raise SystemExit(json.dumps(msg["error"]))
    res = msg.get("result", {})
    if res.get("exceptionDetails"):
        raise SystemExit(json.dumps(res["exceptionDetails"].get("exception", {}).get("description", res["exceptionDetails"])))
    return res.get("result", {}).get("value")


def apply_media(ws, value):
    ws.call(
        "Emulation.setEmulatedMedia",
        {"media": "screen", "features": [{"name": "prefers-color-scheme", "value": value}]},
    )


def apply_size(ws, spec, mobile=False):
    w, h = [int(x) for x in spec.lower().split("x")]
    ws.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": mobile, "screenWidth": w, "screenHeight": h},
    )
    if mobile:
        ws.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
    return w, h


def main(argv):
    if not argv:
        raise SystemExit(__doc__)
    # Emulation overrides live on the CDP session, so every command that cares
    # about viewport or colour scheme must set them in the SAME connection.
    opts = {"size": None, "mobile": False, "media": None}
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--size":
            opts["size"] = argv[i + 1]
            i += 2
        elif a == "--mobile":
            opts["mobile"] = True
            i += 1
        elif a == "--media":
            opts["media"] = argv[i + 1]
            i += 2
        else:
            rest.append(a)
            i += 1
    if not rest:
        raise SystemExit(__doc__)

    cmd = rest[0]
    args = rest[1:]
    ws = connect()
    if opts["size"]:
        apply_size(ws, opts["size"], opts["mobile"])
    if opts["media"]:
        apply_media(ws, opts["media"])

    if cmd == "eval":
        print(json.dumps(evaluate(ws, args[0])))
    elif cmd == "load":
        wait = 2500
        if "--wait" in args:
            wait = int(args[args.index("--wait") + 1])
        url = args[0]
        ws.call("Page.navigate", {"url": url})
        time.sleep(wait / 1000.0)
        print(json.dumps({"url": evaluate(ws, "location.href"), "title": evaluate(ws, "document.title")}))
    elif cmd == "shot":
        path = args[0]
        if "--wait" in args:
            time.sleep(int(args[args.index("--wait") + 1]) / 1000.0)
        params = {"format": "png"}
        if "--full" in args:
            params["captureBeyondViewport"] = True
        msg = ws.call("Page.captureScreenshot", params)
        data = msg["result"]["data"]
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(data))
        print(json.dumps({"path": path, "bytes": os.path.getsize(path)}))
    elif cmd == "size":
        print(json.dumps({"size": opts["size"], "mobile": opts["mobile"]}))
    elif cmd == "media":
        print(json.dumps({"prefers-color-scheme": opts["media"]}))
    elif cmd == "key":
        key = args[0]
        code = key
        text = ""
        if "--code" in args:
            code = args[args.index("--code") + 1]
        if len(key) == 1:
            text = key
        ws.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": key, "code": code, "text": text})
        ws.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": code})
        time.sleep(0.05)
        print(json.dumps({"key": key, "code": code, "active": evaluate(ws, "document.activeElement && (document.activeElement.id || document.activeElement.className || document.activeElement.tagName)")}))
    elif cmd == "url":
        print(json.dumps({"url": evaluate(ws, "location.href")}))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
