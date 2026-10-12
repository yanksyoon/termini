"""Minimal blocking WebSocket client (stdlib only) for probing the controller WS.

Implements just enough of RFC 6455 (client handshake + masked text frames +
server frame parsing) to exercise the controller's streaming endpoints —
``/stream/v1`` (unified event stream) and ``/terminal/v1/nodes/{id}`` — against
either the loopback demo (``ws://``) or the deployed public controller
(``wss://``), independently of any third-party client.

**Scheme handling matters.** A ``wss://`` URL must perform a real TLS handshake
before the HTTP upgrade. Sending the plaintext upgrade bytes into a TLS listener
is answered by the proxy (Caddy / Go ``net/http``) with the canonical

    HTTP/1.0 400 Bad Request
    Client sent an HTTP request to an HTTPS server.

which looks exactly like a failed WebSocket handshake and is easy to
mis-attribute to the server. :meth:`WsClient.connect` now performs TLS for
``wss://`` and names that specific signature when it sees it.

TLS verification is on by default. The deployed controller serves a real
Let's Encrypt certificate, so verification should normally succeed; a
self-signed front (or an IP-addressed host) can be probed with ``verify=False``
or ``ca_file=<path>``, or via the ``TERMINI_WS_INSECURE`` /
``TERMINI_WS_CA_FILE`` environment variables.
"""
from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
from typing import Optional
from urllib.parse import urlsplit

DEFAULT_PORTS = {"ws": 80, "wss": 443}

# The proxy's answer when cleartext HTTP is written into a TLS listener
# (observed verbatim from Caddy: "HTTP/1.0 400 Bad Request"). Named so the
# failure is not mis-read as a rejected WebSocket upgrade.
CLEARTEXT_INTO_TLS_HINT = (
    "the server answered HTTP/1.0 400 Bad Request to a handshake over {scheme}:// — a TLS "
    "listener received a cleartext HTTP request. Check the URL scheme (use wss:// for an "
    "https:// controller) and confirm the TLS handshake is actually happening."
)


class WsClient:
    def __init__(
        self,
        url: str,
        timeout: float = 10.0,
        verify: Optional[bool] = None,
        ca_file: Optional[str] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
    ):
        self.url = url
        self.timeout = timeout
        self.verify = (not _env_true("TERMINI_WS_INSECURE")) if verify is None else verify
        self.ca_file = ca_file or os.environ.get("TERMINI_WS_CA_FILE") or None
        self.ssl_context = ssl_context
        self.sock: Optional[socket.socket] = None
        self.scheme = (urlsplit(url).scheme or "ws").lower()

    # ------------------------------------------------------------------ connect
    def connect(self) -> None:
        u = urlsplit(self.url)
        scheme = self.scheme
        if scheme not in DEFAULT_PORTS:
            raise ValueError(f"unsupported WebSocket scheme {scheme!r}: expected ws or wss")
        host = u.hostname or "127.0.0.1"
        port = u.port or DEFAULT_PORTS[scheme]

        raw = socket.create_connection((host, port), timeout=self.timeout)
        raw.settimeout(self.timeout)
        if scheme == "wss":
            context = self._tls_context()
            try:
                # server_hostname=host sends SNI and drives the hostname check.
                self.sock = context.wrap_socket(raw, server_hostname=host)
            except Exception as exc:  # ssl.SSLError / OSError / cert errors
                raw.close()
                raise ConnectionError(
                    f"TLS handshake with {host}:{port} failed: {type(exc).__name__}: {exc}"
                ) from exc
        else:
            self.sock = raw
        self.sock.settimeout(self.timeout)

        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        host_header = host if port == DEFAULT_PORTS[scheme] else f"{host}:{port}"
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
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
            # Close before raising: a failed probe must not leak the connection
            # (and the caller has no reason to keep it).
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
            raise ConnectionError(self._describe_failure(status_line))
        self._rbuf = bytearray(rest)

    def _tls_context(self) -> ssl.SSLContext:
        if self.ssl_context is not None:
            return self.ssl_context
        if self.verify:
            context = ssl.create_default_context(cafile=self.ca_file)
        else:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def _describe_failure(self, status_line: str) -> str:
        msg = f"websocket handshake failed: {status_line}"
        # Go's net/http (Caddy) answers cleartext-on-a-TLS-port with exactly this.
        if " 400 " in status_line and status_line.startswith("HTTP/1.0"):
            return msg + " — " + CLEARTEXT_INTO_TLS_HINT.format(scheme=self.scheme)
        return msg

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


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")
