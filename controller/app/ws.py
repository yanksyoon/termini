"""Minimal WebSocket server (RFC 6455) over the stdlib HTTP server.

The controller is stdlib-only (no `websockets`), so this hand-rolls the wire
protocol the dashboard and terminal need: the opening handshake, text-frame
send, and masked client-frame receive (with ping/pong and close handling).

Frames are UTF-8 text; each frame is one complete JSON object (controller-api.md
§1.4). Binary payloads (terminal bytes) are base64-encoded inside the JSON
``data`` field, so we never send or receive binary WS frames.
"""
from __future__ import annotations

import base64
import hashlib
import struct
import threading
from typing import BinaryIO

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(Exception):
    pass


class ConnectionClosed(Exception):
    def __init__(self, code: int = 1000, reason: str = "") -> None:
        super().__init__(reason or f"connection closed ({code})")
        self.code = code
        self.reason = reason


def accept_key(client_key: str) -> str:
    """Compute the Sec-WebSocket-Accept value for the opening handshake."""
    digest = hashlib.sha1((client_key + WS_GUID).encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


class WebSocket:
    """Server-side WebSocket bound to an HTTP handler's rfile/socket."""

    def __init__(self, rfile: BinaryIO, sock) -> None:
        self._rfile = rfile
        self._sock = sock
        self._send_lock = threading.Lock()
        self.closed = False

    def fileno(self) -> int:
        """Underlying socket fd, for non-blocking readability checks."""
        return self._sock.fileno()

    # ---------------------------------------------------------------- receive
    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._rfile.read(n - len(buf))
            if not chunk:
                raise ConnectionClosed(1006, "eof")
            buf.extend(chunk)
        return bytes(buf)

    def recv_message(self) -> str:
        """Read and reassemble one complete text message.

        Control frames are handled inline: ping → pong, close → close handshake
        + raise :class:`ConnectionClosed`, binary frames raise (unsupported).
        Returns the decoded UTF-8 text payload.
        """
        fragments: list[bytes] = []
        while True:
            opcode, payload, fin = self._recv_frame()
            if opcode == OP_PING:
                self.send_pong(payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                code = 1000
                if len(payload) >= 2:
                    code = struct.unpack("!H", payload[:2])[0]
                self._send_frame(OP_CLOSE, payload[:2])
                self.closed = True
                raise ConnectionClosed(code, payload[2:].decode("utf-8", "replace"))
            if opcode == OP_BINARY:
                raise WebSocketError("binary frames are not supported; base64-encode in JSON data")
            if opcode == OP_CONT:
                fragments.append(payload)
            elif opcode == OP_TEXT:
                fragments.append(payload)
            else:
                raise WebSocketError(f"unsupported opcode {opcode}")
            if fin:
                break
        return b"".join(fragments).decode("utf-8", "replace")

    def _recv_frame(self) -> tuple[int, bytes, bool]:
        b0, b1 = self._read_exact(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        if length > 16 * 1024 * 1024:
            raise WebSocketError("frame too large")
        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(length)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload, fin

    # ------------------------------------------------------------------- send
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        b0 = 0x80 | opcode  # FIN always set; server frames are never fragmented
        n = len(payload)
        header = bytearray([b0])
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += struct.pack("!H", n)
        else:
            header.append(127)
            header += struct.pack("!Q", n)
        with self._send_lock:
            self._sock.sendall(bytes(header) + payload)

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def send_pong(self, payload: bytes = b"") -> None:
        self._send_frame(OP_PONG, payload)

    def send_close(self, code: int = 1000, reason: str = "") -> None:
        payload = struct.pack("!H", code) + reason.encode("utf-8")
        try:
            self._send_frame(OP_CLOSE, payload)
        except OSError:
            pass
        self.closed = True
