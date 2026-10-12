"""TLS-aware WebSocket probe client (``w7/acceptance/lib/ws.py``).

Regression cover for the misdiagnosed "``/stream/v1`` returns HTTP 400" finding.
The stream *server* was fine: the probe client dialled the controller's TLS
port and wrote a **cleartext** HTTP upgrade, which Caddy (Go ``net/http``)
answers with ``HTTP/1.0 400 Bad Request`` — a response that reads like a
rejected WebSocket handshake and was attributed to the proxy or the controller.

These tests prove, hermetically (stdlib only, an ephemeral self-signed cert):

  * ``wss://`` performs a real TLS handshake (certificate verification is ON by
    default — a self-signed front is *rejected* unless its CA is supplied);
  * ``ws://`` still works against the loopback demo (no accidental behaviour
    change for the existing harness path);
  * the cleartext-into-a-TLS-port 400 is reported with the scheme/TLS cause
    named, so it can never again be mis-read as a failed upgrade.

No network beyond loopback; no third-party packages.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
WS_PY = REPO / "w7" / "acceptance" / "lib" / "ws.py"


def _load_ws_module():
    """Load lib/ws.py by path (no sys.path games, no `lib` name collision)."""
    spec = importlib.util.spec_from_file_location("termini_ws_probe_lib", WS_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wsmod = _load_ws_module()

# The verbatim answer Caddy/Go writes when cleartext HTTP reaches a TLS listener
# (observed on the deployed controller: `HTTP/1.0 400 Bad Request`).
GO_CLEARTEXT_400 = b"HTTP/1.0 400 Bad Request\r\n\r\nClient sent an HTTP request to an HTTPS server."


def _server_frame(payload: bytes) -> bytes:
    """One unmasked text frame (server -> client)."""
    header = bytearray([0x81])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += n.to_bytes(2, "big")
    else:
        header.append(127)
        header += n.to_bytes(8, "big")
    return bytes(header) + payload


class _FakeWsServer:
    """Loopback WS server in two flavours: TLS (wss) and plaintext (ws)."""

    def __init__(self, tls: bool, certfile: str | None = None, keyfile: str | None = None,
                 payload: dict | None = None):
        self.tls = tls
        self.payload = payload or {"event": "log.line", "data": {"message": "hello"}}
        self.requests: list[bytes] = []
        self.tls_negotiated = 0
        self._ctx = None
        if tls:
            assert certfile is not None and keyfile is not None
            self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self.sock.settimeout(0.3)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            if self._ctx is not None:
                try:
                    conn = self._ctx.wrap_socket(conn, server_side=True)
                except (ssl.SSLError, OSError):
                    conn.close()
                    return
                self.tls_negotiated += 1
            conn.settimeout(5)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    conn.close()
                    return
                buf += chunk
            self.requests.append(buf)
            key = ""
            for line in buf.split(b"\r\n"):
                if line.lower().startswith(b"sec-websocket-key:"):
                    key = line.split(b":", 1)[1].strip().decode()
            accept = _accept(key)
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n"
            )
            conn.sendall(_server_frame(json.dumps(self.payload).encode()))
        except (OSError, ssl.SSLError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


class _CleartextToTlsPortServer:
    """Reproduces Caddy/Go's answer to a cleartext request on a TLS port.

    Any bytes are answered with the exact ``HTTP/1.0 400 Bad Request`` line,
    which is what a no-TLS WS probe saw when pointed at ``wss://``.
    """

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        self.sock.settimeout(0.3)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(5)
                conn.recv(4096)
                conn.sendall(GO_CLEARTEXT_400)
            except OSError:
                pass
            finally:
                conn.close()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


def _accept(key: str) -> str:
    import base64
    import hashlib
    guid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    return base64.b64encode(hashlib.sha1((key + guid).encode()).digest()).decode()


@unittest.skipUnless(shutil.which("openssl"), "openssl not available to mint a test certificate")
class WsClientTlsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="termini-ws-tls-")
        cls.cert = os.path.join(cls.tmp, "test-cert.pem")
        cls.key = os.path.join(cls.tmp, "test-key.pem")
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", cls.key, "-out", cls.cert, "-days", "2",
             "-subj", "/CN=localhost",
             "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
            check=True, capture_output=True,
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -------------------------------------------------------------- TLS is real
    def test_wss_does_a_tls_handshake_and_rejects_an_untrusted_cert(self):
        srv = _FakeWsServer(tls=True, certfile=self.cert, keyfile=self.key)
        try:
            client = wsmod.WsClient(f"wss://localhost:{srv.port}/stream/v1?token=t")
            with self.assertRaises(ConnectionError) as ctx:
                client.connect()
            self.assertIn("TLS handshake", str(ctx.exception))
            self.assertIn("SSL", str(ctx.exception).upper())
            # The server never completed a TLS handshake, so it saw no request.
            self.assertEqual(srv.tls_negotiated, 0)
        finally:
            srv.stop()

    def test_wss_with_trusted_ca_upgrades_to_101(self):
        srv = _FakeWsServer(tls=True, certfile=self.cert, keyfile=self.key)
        try:
            client = wsmod.WsClient(f"wss://localhost:{srv.port}/stream/v1?token=t",
                                    ca_file=self.cert)
            client.connect()  # no raise == 101
            self.assertEqual(srv.tls_negotiated, 1)
            frame = client.recv_frame(timeout=3)
            client.close()
            self.assertIsNotNone(frame)
            self.assertEqual(json.loads(frame["data"])["event"], "log.line")
            self.assertIn(b"/stream/v1?token=t", srv.requests[0])
        finally:
            srv.stop()

    def test_wss_verify_false_still_negotiates_tls(self):
        srv = _FakeWsServer(tls=True, certfile=self.cert, keyfile=self.key)
        try:
            client = wsmod.WsClient(f"wss://localhost:{srv.port}/stream/v1?token=t", verify=False)
            client.connect()
            client.close()
            self.assertEqual(srv.tls_negotiated, 1)
        finally:
            srv.stop()

    # ------------------------------------------------------ unchanged ws:// path
    def test_plaintext_ws_still_works(self):
        srv = _FakeWsServer(tls=False)
        try:
            client = wsmod.WsClient(f"ws://127.0.0.1:{srv.port}/terminal/v1/nodes/x?token=t")
            client.connect()
            frame = client.recv_frame(timeout=3)
            client.close()
            self.assertIsNotNone(frame)
            self.assertEqual(srv.tls_negotiated, 0)
        finally:
            srv.stop()

    # ------------------------------------------- the misdiagnosed failure, named
    def test_cleartext_400_is_reported_as_a_tls_cause(self):
        srv = _CleartextToTlsPortServer()
        try:
            client = wsmod.WsClient(f"ws://127.0.0.1:{srv.port}/stream/v1?token=t")
            with self.assertRaises(ConnectionError) as ctx:
                client.connect()
            msg = str(ctx.exception)
            self.assertIn("websocket handshake failed", msg)
            self.assertIn("400", msg)
            # The diagnostic must name the real cause, not leave it ambiguous.
            self.assertIn("cleartext", msg.lower())
            self.assertIn("wss://", msg)
        finally:
            srv.stop()

    def test_unsupported_scheme_is_rejected(self):
        client = wsmod.WsClient("http://127.0.0.1:1/stream/v1")
        with self.assertRaises(ValueError):
            client.connect()


if __name__ == "__main__":
    unittest.main(verbosity=2)
