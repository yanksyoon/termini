"""Minimal blocking WebSocket client (stdlib only) for probing the terminal WS.

Implements just enough of RFC 6455 (client handshake + masked text frames +
server frame parsing) to exercise the controller's /terminal/v1 endpoint
independently of any third-party client.
"""
from __future__ import annotations

import base64
import os
import socket
import struct
from typing import Optional
from urllib.parse import urlsplit


class WsClient:
    def __init__(self, url: str, timeout: float = 10.0):
        self.url = url
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None

    def connect(self) -> None:
        u = urlsplit(self.url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "wss" else 80)
        self.sock = socket.create_connection((host, port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)

        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(req.encode())

        # read handshake response headers
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake: connection closed")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status_line:
            raise ConnectionError(f"websocket handshake failed: {status_line}")
        self._rbuf = bytearray(rest)

    def _recv_exact(self, n: int) -> bytes:
        while len(self._rbuf) < n:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket: connection closed")
            self._rbuf.extend(chunk)
        out = bytes(self._rbuf[:n])
        del self._rbuf[:n]
        return out

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x81])  # FIN + text
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", n))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", n))
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv_frame(self, timeout: Optional[float] = None) -> Optional[dict]:
        """Return {opcode, data} or None on clean close. Raises on timeout."""
        self.sock.settimeout(timeout if timeout is not None else self.timeout)
        try:
            b1, b2 = self._recv_exact(2)
        except socket.timeout:
            raise
        fin = b1 & 0x80
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        mask_key = self._recv_exact(4) if masked else None
        data = self._recv_exact(length)
        if mask_key:
            data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
        if opcode == 0x8:  # close
            return None
        return {"fin": fin, "opcode": opcode, "data": data.decode("utf-8", errors="replace")}

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
