"""Integration + unit tests for the web terminal and SSH access string UX.

Covers the frozen terminal contract (docs/contracts/dashboard.md §3) end to end
against the stdlib dev bridge in ``frontend/server/terminal_bridge.py``:

  * the WebSocket handshake, ``ready`` frame, input/resize framing and output
    relay against a real PTY shell (``whoami`` / ``ls`` actually run);
  * auth rejection (WS close 4401) and unreachable-node handling (NODE_OFFLINE);
  * single-session-per-node replacement (SESSION_REPLACED);
  * the REST endpoints the UI consumes (node list, SSH access payload);
  * the JS unit suite (framing / sanitize / session state machine) via node.

Run locally:  python3 -m unittest discover -s tests -v   (from repo root)
"""
from __future__ import annotations

import base64
import getpass
import glob
import importlib.util
import json
import os
import pathlib
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import unittest
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
BRIDGE_PATH = REPO / "frontend" / "server" / "terminal_bridge.py"
TEST_TOKEN = "test-project-token"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("termini_mock_bridge", BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class WSError(Exception):
    pass


class WSClient:
    """Minimal RFC 6455 client, just enough to drive the bridge in tests."""

    def __init__(self, host, port, path, timeout=10.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.rfile = self.sock.makefile("rb")
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode("ascii"))
        self.status_line = self.rfile.readline().decode("ascii").strip()
        self.headers: dict[str, str] = {}
        while True:
            line = self.rfile.readline().decode("ascii").strip()
            if not line:
                break
            k, _, v = line.partition(":")
            self.headers[k.lower()] = v.strip()

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        header += mask
        masked = bytes(payload[i] ^ mask[i % 4] for i in range(n))
        self.sock.sendall(bytes(header) + masked)

    def send_json(self, obj) -> None:
        self.send_text(json.dumps(obj))

    def recv_frame(self):
        head = self.rfile.read(2)
        if len(head) < 2:
            raise WSError("socket closed")
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self.rfile.read(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.rfile.read(8))[0]
        mask = self.rfile.read(4) if masked else b""
        data = self.rfile.read(length) if length else b""
        if masked:
            data = bytes(data[i] ^ mask[i % 4] for i in range(length))
        return opcode, data

    def recv_json(self):
        """Return the next JSON text frame, skipping ping/pong control frames."""
        while True:
            opcode, data = self.recv_frame()
            if opcode == 0x1:
                return json.loads(data.decode("utf-8"))
            if opcode == 0x9:
                continue
            if opcode == 0x8:
                code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else None
                raise WSError(f"closed {code}: {data[2:].decode('utf-8', 'replace')}")

    def recv_until(self, predicate, limit=200):
        """Collect output frames until predicate(decoded_text) is true."""
        text = ""
        for _ in range(limit):
            frame = self.recv_json()
            if frame.get("type") == "output":
                text += base64.b64decode(frame["data"]).decode("utf-8", "replace")
                if predicate(text):
                    return text
            elif frame.get("type") == "error":
                raise WSError(f"error frame: {frame}")
        raise WSError(f"predicate not satisfied; saw:\n{text}")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class WebTerminalBridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_bridge()
        shell = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
        cls.srv = cls.mod.build_server("127.0.0.1", 0, TEST_TOKEN, shell)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.nodes = {n["node_id"]: n for n in cls.srv.nodes}
        cls.online = next(n["node_id"] for n in cls.srv.nodes if not n["offline"])
        cls.offline = next(n["node_id"] for n in cls.srv.nodes if n["offline"])

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def ws(self, node_id, token=TEST_TOKEN):
        return WSClient("127.0.0.1", self.port, f"/terminal/v1/{node_id}?token={token}")

    def http_json(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
            return r.status, json.loads(r.read())

    # ---------------------------------------------------------------- REST
    def test_node_list_endpoint(self):
        status, body = self.http_json("/v1/projects/demo/nodes")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(body["nodes"]), 2)
        for n in body["nodes"]:
            self.assertIn("node_id", n)
            self.assertIn("state", n)

    def test_ssh_endpoint_matches_contract_shape(self):
        status, body = self.http_json(f"/v1/projects/demo/nodes/{self.online}/ssh")
        self.assertEqual(status, 200)
        for field in ("node_id", "user", "host", "port", "key_fingerprint", "command"):
            self.assertIn(field, body)
        self.assertEqual(body["user"], "ubuntu")
        self.assertTrue(body["command"].startswith("ssh -i "))
        self.assertIn(f"ubuntu@{body['host']}", body["command"])

    # ------------------------------------------------------------ handshake
    def test_ready_frame_and_interactive_shell(self):
        c = self.ws(self.online)
        try:
            ready = c.recv_json()
            self.assertEqual(ready["type"], "ready")
            self.assertEqual(ready["node_id"], self.online)
            self.assertTrue(ready["user"])
            self.assertTrue(ready["shell"])

            # resize frame is accepted without error
            c.send_json({"type": "resize", "cols": 100, "rows": 30})

            marker = "TERMINI_PROBE_42"
            c.send_json({"type": "input", "data": base64.b64encode(f"echo {marker}\n".encode()).decode()})
            out = c.recv_until(lambda t: marker in t)
            self.assertIn(marker, out)

            # whoami actually runs
            c.send_json({"type": "input", "data": base64.b64encode(b"whoami\n").decode()})
            who = c.recv_until(lambda t: getpass.getuser() in t)
            self.assertIn(getpass.getuser(), who)

            # ls actually runs against a real directory
            with tempfile.TemporaryDirectory() as d:
                probe = pathlib.Path(d) / "termini_probe.txt"
                probe.write_text("hi\n")
                c.send_json({"type": "input", "data": base64.b64encode(f"ls {d}\n".encode()).decode()})
                out = c.recv_until(lambda t: "termini_probe.txt" in t)
                self.assertIn("termini_probe.txt", out)
        finally:
            c.close()

    def test_ping_is_answered_with_pong(self):
        c = self.ws(self.online)
        try:
            c.recv_json()  # ready
            c.send_json({"type": "ping"})
            # skip any output frames until the pong
            for _ in range(50):
                frame = c.recv_json()
                if frame.get("type") == "pong":
                    return
            self.fail("no pong received")
        finally:
            c.close()

    # -------------------------------------------------------------- errors
    def test_bad_token_is_rejected_with_4401(self):
        c = self.ws(self.online, token="wrong-token")
        try:
            with self.assertRaises(WSError) as cm:
                c.recv_json()
            self.assertIn("4401", str(cm.exception))
        finally:
            c.close()

    def test_unknown_node_returns_not_found(self):
        c = self.ws("01NOPE000000000000000000000000")
        try:
            frame = c.recv_json()
            self.assertEqual(frame["type"], "error")
            self.assertEqual(frame["code"], "NOT_FOUND")
        finally:
            c.close()

    def test_offline_node_returns_node_offline(self):
        c = self.ws(self.offline)
        try:
            frame = c.recv_json()
            self.assertEqual(frame["type"], "error")
            self.assertEqual(frame["code"], "NODE_OFFLINE")
        finally:
            c.close()

    def test_second_session_replaces_the_first(self):
        first = self.ws(self.online)
        second = None
        try:
            first.recv_json()  # ready
            second = self.ws(self.online)
            second.recv_json()  # ready (replacement proceeds)
            got = None
            for _ in range(50):
                frame = first.recv_json()
                if frame.get("type") == "error":
                    got = frame
                    break
            self.assertIsNotNone(got, "first session never received an error frame")
            assert got is not None
            self.assertEqual(got["code"], "SESSION_REPLACED")
        finally:
            first.close()
            if second:
                second.close()

    # ------------------------------------------------------------- JS suite
    def test_js_unit_suite_passes(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        files = sorted(glob.glob(str(REPO / "frontend" / "test" / "*.test.mjs")))
        self.assertTrue(files, "no JS test files found")
        proc = subprocess.run(
            [node, "--test", *files], cwd=str(REPO), capture_output=True, text=True, timeout=120
        )
        self.assertEqual(proc.returncode, 0, f"node --test failed:\n{proc.stdout}\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
