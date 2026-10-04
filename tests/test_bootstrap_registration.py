"""Unit tests for the termini-bootstrap registration retry policy (t_ca7fc132).

Covers the W7 gate findings F1/F2 at the unit level — deterministic and fast,
independent of LXD:

  F1: a 401/403 (consumed/invalid token) must reach terminal REGISTRATION_FAILED
      after a bounded number of backoff retries, never loop forever.
  F2: a DNS failure or a refused connection must fail fast to a terminal state,
      not sit silently in REGISTERING.

The end-to-end proof on a real node is `w7/redteam/boot/boot_latency.py`.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import socket
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
DAEMON = REPO / "infra" / "bootstrap" / "node" / "termini-bootstrap.py"


def load_daemon(env: dict[str, str]):
    """Load the daemon as a module with paths redirected into a temp dir."""
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location("termini_bootstrap_test", DAEMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class _NoSleep:
    """Proxy for the stdlib ``time`` module whose ``sleep`` is a no-op.

    Assigned to the *daemon module's* ``time`` attribute only — a bare
    ``mod.time.sleep = ...`` would mutate the shared stdlib module and leak into
    other tests in the same process (it broke a sibling fsync-timing test).
    """

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def sleep(self, _seconds):  # noqa: D401 - test double
        return None


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.mod = load_daemon({
            "TERMINI_STATE_PATH": str(root / "state.json"),
            "TERMINI_IDENTITY_DIR": str(root / "identity"),
            "TERMINI_SPOOL_DIR": str(root / "telemetry"),
            "TERMINI_TERMINAL_HOLD_S": "0.01",   # bound the terminal hold
        })

    def state(self):
        return self.mod.read_json(self.mod.STATE_PATH)


class TestClassification(_Base):
    def test_401_is_terminal_auth(self):
        e = self.mod.classify_http_error(401, {"error": {"code": "UNAUTHENTICATED"}})
        self.assertEqual(e.kind, "auth")
        self.assertTrue(e.terminal)

    def test_403_is_terminal_auth(self):
        e = self.mod.classify_http_error(403, {"error": {"code": "PERMISSION_DENIED"}})
        self.assertEqual(e.kind, "auth")
        self.assertTrue(e.terminal)

    def test_5xx_is_transient(self):
        e = self.mod.classify_http_error(503, {"error": {"code": "INTERNAL"}})
        self.assertEqual(e.kind, "server")
        self.assertFalse(e.terminal)

    def test_429_is_transient(self):
        e = self.mod.classify_http_error(429, {"error": {"code": "RATE_LIMITED"}})
        self.assertEqual(e.kind, "rate_limited")
        self.assertFalse(e.terminal)

    def test_dns_error_is_terminal(self):
        exc = urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
        e = self.mod.classify_network_error(exc)
        self.assertEqual(e.kind, "unreachable_dns")
        self.assertTrue(e.terminal)

    def test_refused_error_is_terminal(self):
        exc = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        e = self.mod.classify_network_error(exc)
        self.assertEqual(e.kind, "unreachable_refused")
        self.assertTrue(e.terminal)

    def test_reset_error_is_terminal(self):
        exc = urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))
        e = self.mod.classify_network_error(exc)
        self.assertEqual(e.kind, "unreachable_refused")
        self.assertTrue(e.terminal)

    def test_timeout_is_terminal(self):
        e = self.mod.classify_network_error(urllib.error.URLError(TimeoutError("timed out")))
        self.assertEqual(e.kind, "unreachable_timeout")
        self.assertTrue(e.terminal)


class TestRetryLoop(_Base):
    def _stub_register(self, exc):
        calls = {"n": 0}

        def fake(cfg, node_name, token):
            calls["n"] += 1
            raise exc
        self.mod.register = fake
        # no real backoff in tests; swap the module's time reference, not the
        # shared stdlib module (see _NoSleep)
        self.mod.time = _NoSleep(self.mod.time)
        return calls

    def test_consumed_token_reaches_terminal_after_le_3_retries(self):
        calls = self._stub_register(self.mod.RegistrationError("auth", 401, "HTTP 401 UNAUTHENTICATED"))
        out = self.mod.register_with_retries({}, "node-x", "tok")
        self.assertIsNone(out)
        st = self.state()
        self.assertEqual(st["state"], "REGISTRATION_FAILED")
        self.assertEqual(st["failure_kind"], "auth")
        self.assertTrue(st["terminal"])
        self.assertEqual(calls["n"], 4)          # initial + at most 3 backoff retries
        self.assertLessEqual(calls["n"], 4)

    def test_dns_failure_fails_fast_to_terminal(self):
        exc = self.mod.RegistrationError("unreachable_dns", None, "DNS name could not be resolved")
        self._stub_register(exc)
        out = self.mod.register_with_retries({}, "node-x", "tok")
        self.assertIsNone(out)
        st = self.state()
        self.assertEqual(st["state"], "REGISTRATION_FAILED")
        self.assertEqual(st["failure_kind"], "unreachable_dns")

    def test_refused_fails_fast_to_terminal(self):
        exc = self.mod.RegistrationError("unreachable_refused", None, "connection refused")
        self._stub_register(exc)
        out = self.mod.register_with_retries({}, "node-x", "tok")
        self.assertIsNone(out)
        self.assertEqual(self.state()["state"], "REGISTRATION_FAILED")

    def test_5xx_retries_indefinitely_and_stays_registering(self):
        exc = self.mod.RegistrationError("server", 503, "HTTP 503 INTERNAL")
        self._stub_register(exc)
        sentinel = RuntimeError("enough")

        def sleep_then_stop(_s):
            raise sentinel
        self.mod.time.sleep = sleep_then_stop
        with self.assertRaises(RuntimeError):
            self.mod.register_with_retries({}, "node-x", "tok")
        st = self.state()
        self.assertEqual(st["state"], "REGISTERING")     # not terminal
        self.assertNotEqual(st["state"], "REGISTRATION_FAILED")

    def test_custom_max_retries_respected(self):
        calls = self._stub_register(self.mod.RegistrationError("auth", 401, "HTTP 401"))
        out = self.mod.register_with_retries({"registration_max_retries": "5"}, "node-x", "tok")
        self.assertIsNone(out)
        self.assertEqual(calls["n"], 6)                  # initial + 5 retries
        self.assertEqual(self.state()["registration_attempts"], 6)


class _Auth401Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = b'{"error": {"code": "UNAUTHENTICATED", "message": "bootstrap token already used"}}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        return


class TestRegisterAgainstRealHTTP(_Base):
    def test_register_maps_real_401(self):
        srv = HTTPServer(("127.0.0.1", 0), _Auth401Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(srv.shutdown)
        cfg = {
            "controller_url": f"http://127.0.0.1:{srv.server_address[1]}",
            "node_name": "unit-node",
            "register_timeout_s": "3",
        }
        with self.assertRaises(self.mod.RegistrationError) as cm:
            self.mod.register(cfg, "unit-node", "consumed-token")
        self.assertEqual(cm.exception.kind, "auth")
        self.assertEqual(cm.exception.status, 401)
        self.assertTrue(cm.exception.terminal)


if __name__ == "__main__":
    unittest.main()
