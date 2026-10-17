"""A node must come up with its OWN LXD API, and registration must fail closed without one.

MEASURED DEFECT (live standing fleet, termini-node-a, 2026-10-17). The node's
`core.https_address` was EMPTY and nothing listened on `:8443`; from the controller
`https://10.10.0.11:8443/1.0` returned `000` (connect failure) and 183 of the last
200 operator executions read `URLError: <urlopen error [Errno 111] Connection
refused>`. Nothing in provisioning guaranteed the listener: it existed for a while
(12:31-12:38Z returned real operation ids) and then drifted away with the daemon's
database.

What is pinned here:

  1. the endpoint a node advertises is the node's OWN `host:port` — never loopback,
     never a value it has not proved answers;
  2. registration FAILS with the distinct kind `lxd_api_unreachable` / state
     `LXD_API_UNAVAILABLE` when the listener cannot be brought up, and the unit
     never reports READY — a node that cannot be remediated must not look healthy;
  3. the listener is repaired through the daemon's own unix socket (the `lxc` CLI
     cannot run under this unit: NoNewPrivileges), and the endpoint is advertised
     only after something answered on it;
  4. the heartbeat carries the endpoint, so a node that came up later still stops
     the controller falling back to the one global `TERMINI_LXD_BASE_URL`.

The end-to-end proof on the live fleet is in the task's completion metadata.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
DAEMON = REPO / "infra" / "bootstrap" / "node" / "termini-bootstrap.py"


def load_daemon(env: dict[str, str]):
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location("termini_bootstrap_lxdapi_test", DAEMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class _NoSleep:
    """Module-local time proxy with a no-op sleep (never mutate stdlib time)."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def sleep(self, _seconds):
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
            "TERMINI_TERMINAL_HOLD_S": "0.01",
            "TERMINI_LXD_API_PROBE_TIMEOUT_S": "0.3",
            "TERMINI_LXD_API_READY_WAIT_S": "0.05",
            "TERMINI_LXD_API_READY_POLL_S": "0",
        })
        self.mod.time = _NoSleep(self.mod.time)
        self.mod.primary_ipv4 = lambda: "10.10.0.11"
        # Never let a unit test talk to a REAL LXD: the daemon under test repairs
        # the listener through the local unix socket, and a dev/CI host that runs
        # LXD would have its own core.https_address rewritten by this suite.
        self.mod.LXD_UNIX_SOCKETS = (str(root / "no-such-lxd.socket"),)
        self.mod.start_lxd_daemon = lambda: None

    def state(self):
        return self.mod.read_json(self.mod.STATE_PATH)


class EndpointDerivation(_Base):
    def test_endpoint_is_the_nodes_own_underlay_address(self):
        self.assertEqual(self.mod.own_lxd_endpoint({}), "10.10.0.11:8443")

    def test_endpoint_honours_the_configured_port(self):
        self.assertEqual(self.mod.own_lxd_endpoint({"lxd_port": "9999"}), "10.10.0.11:9999")

    def test_port_falls_back_to_8443_on_junk(self):
        for junk in ("", "abc", "0", "-1", "70000", None):
            self.assertEqual(self.mod.lxd_port({"lxd_port": junk}), 8443, junk)

    def test_registration_body_only_advertises_a_verified_endpoint(self):
        self.assertNotIn("endpoint", self.mod.registration_body({}, "node-x")["machine"]["lxd"])
        body = self.mod.registration_body({}, "node-x", lxd_endpoint="10.10.0.11:8443")
        self.assertEqual(body["machine"]["lxd"]["endpoint"], "10.10.0.11:8443")


class Probe(_Base):
    def test_nothing_listening_is_not_ok(self):
        # Pick a port that is certainly closed on loopback.
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        ok, detail = self.mod.probe_lxd_api("127.0.0.1", port)
        self.assertFalse(ok)
        self.assertIn("Error", detail)

    def test_any_http_status_proves_a_listener(self):
        class _FakeResponse:
            status = 403

            def read(self, _n=None):
                return b'{"error":"untrusted"}'

        class _FakeConn:
            def __init__(self, *_a, **_kw):
                self.closed = False

            def request(self, method, path, body=None, headers=None):
                assert (method, path) == ("GET", "/1.0")

            def getresponse(self):
                return _FakeResponse()

            def close(self):
                self.closed = True

        real = self.mod.http.client.HTTPSConnection
        self.mod.http.client.HTTPSConnection = _FakeConn
        try:
            ok, detail = self.mod.probe_lxd_api("10.10.0.11", 8443)
        finally:
            self.mod.http.client.HTTPSConnection = real
        self.assertTrue(ok)
        self.assertEqual(detail, "HTTP 403")


class EnsureLxdApi(_Base):
    def test_already_listening_is_left_alone(self):
        self.mod.probe_lxd_api = lambda host, port, timeout=0: (True, "HTTP 200")
        called = {"socket": 0}
        self.mod._lxd_socket_request = lambda *a, **k: called.__setitem__("socket", called["socket"] + 1)
        res = self.mod.ensure_lxd_api({})
        self.assertTrue(res["ok"])
        self.assertFalse(res["repaired"])
        self.assertEqual(res["endpoint"], "10.10.0.11:8443")
        self.assertEqual(called["socket"], 0)

    def test_binds_the_listener_over_the_daemon_socket(self):
        calls = {"probe": 0, "put": []}

        def probe(host, port, timeout=0):
            calls["probe"] += 1
            return (calls["probe"] > 1, "HTTP 200" if calls["probe"] > 1 else "ConnectionRefusedError")

        def socket_put(method, path, body=None, timeout=None):
            calls["put"].append((method, path, json.dumps(body)))
            return 200, {}, "/var/snap/lxd/common/lxd/unix.socket"

        self.mod.probe_lxd_api = probe
        self.mod._lxd_socket_request = socket_put
        res = self.mod.ensure_lxd_api({})
        self.assertTrue(res["ok"])
        self.assertTrue(res["repaired"])
        self.assertEqual(calls["put"][0][0], "PUT")
        self.assertEqual(calls["put"][0][1], "/1.0")
        self.assertIn('"core.https_address": ":8443"', calls["put"][0][2])

    def test_starts_the_daemon_when_its_socket_is_dead(self):
        started = {"units": []}
        self.mod.probe_lxd_api = lambda host, port, timeout=0: (False, "ConnectionRefusedError")
        self.mod._lxd_socket_request = lambda *a, **k: (0, None, "no LXD unix socket found")
        self.mod.start_lxd_daemon = lambda: started["units"].append("snap.lxd.daemon.service")
        res = self.mod.ensure_lxd_api({})
        self.assertFalse(res["ok"])
        self.assertTrue(res["repaired"])
        self.assertEqual(started["units"], ["snap.lxd.daemon.service"])


class RegistrationFailsClosed(_Base):
    def test_no_listener_means_no_registration_attempt(self):
        """The enrollment token is not spent: there is no HTTP call at all."""
        self.mod.ensure_lxd_api = lambda cfg: {
            "ok": False, "endpoint": "10.10.0.11:8443", "repaired": True,
            "detail": "ConnectionRefusedError: [Errno 111] Connection refused",
        }
        posted = {"n": 0}

        def fake_http(*a, **k):
            posted["n"] += 1
            return 201, {"node_id": "01M4"}

        self.mod.http_json = fake_http
        with self.assertRaises(self.mod.RegistrationError) as cm:
            self.mod.register({"controller_url": "http://127.0.0.1:1"}, "node-x", "tok")
        self.assertEqual(cm.exception.kind, "lxd_api_unreachable")
        self.assertTrue(cm.exception.terminal)
        self.assertEqual(posted["n"], 0)
        self.assertIn("10.10.0.11:8443", str(cm.exception))

    def test_parks_in_the_distinct_lxd_api_unavailable_state(self):
        self.mod.register = lambda cfg, node, tok: (_ for _ in ()).throw(
            self.mod.RegistrationError("lxd_api_unreachable", None, "no listener"))
        out = self.mod.register_with_retries({}, "node-x", "tok")
        self.assertIsNone(out)
        st = self.state()
        self.assertEqual(st["state"], self.mod.LXD_API_FAILED_STATE)
        self.assertEqual(st["state"], "LXD_API_UNAVAILABLE")
        self.assertNotEqual(st["state"], "REGISTRATION_FAILED")
        self.assertEqual(st["failure_kind"], "lxd_api_unreachable")
        self.assertTrue(st["terminal"])

    def test_other_terminal_kinds_keep_registration_failed(self):
        self.mod.register = lambda cfg, node, tok: (_ for _ in ()).throw(
            self.mod.RegistrationError("auth", 401, "HTTP 401"))
        self.assertIsNone(self.mod.register_with_retries({}, "node-x", "tok"))
        self.assertEqual(self.state()["state"], "REGISTRATION_FAILED")


class _HeartbeatSink(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length)
        type(self).received.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": json.loads(raw or b"{}"),
        })
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        return


class HeartbeatCarriesTheEndpoint(_Base):
    def _sink(self):
        _HeartbeatSink.received = []
        srv = HTTPServer(("127.0.0.1", 0), _HeartbeatSink)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        return srv

    def test_listening_node_advertises_its_endpoint(self):
        self.mod.lxd_api_report = lambda cfg: {
            "listening": True, "endpoint": "10.10.0.11:8443", "detail": "HTTP 200"}
        srv = self._sink()
        url = f"http://127.0.0.1:{srv.server_address[1]}/v1/nodes/01M4/heartbeat"
        self.mod.send_heartbeat({}, url, "node-token")
        self.assertEqual(len(_HeartbeatSink.received), 1)
        beat = _HeartbeatSink.received[0]
        self.assertEqual(beat["path"], "/v1/nodes/01M4/heartbeat")
        self.assertEqual(beat["auth"], "Bearer node-token")
        self.assertEqual(beat["body"]["lxd_endpoint"], "10.10.0.11:8443")
        self.assertTrue(beat["body"]["lxd_api"]["listening"])

    def test_dead_listener_advertises_nothing(self):
        self.mod.lxd_api_report = lambda cfg: {
            "listening": False, "endpoint": "10.10.0.11:8443",
            "detail": "ConnectionRefusedError: [Errno 111]"}
        srv = self._sink()
        url = f"http://127.0.0.1:{srv.server_address[1]}/v1/nodes/01M4/heartbeat"
        self.mod.send_heartbeat({}, url, "node-token")
        body = _HeartbeatSink.received[0]["body"]
        self.assertNotIn("lxd_endpoint", body)
        self.assertFalse(body["lxd_api"]["listening"])
        self.assertIn("ConnectionRefusedError", body["lxd_api"]["detail"])


class HeartbeatInterval(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mod = load_daemon({})

    def test_default_and_floor(self):
        self.assertEqual(self.mod.heartbeat_interval_s({}), 15.0)
        self.assertEqual(self.mod.heartbeat_interval_s({"heartbeat_interval_s": "1"}), 5.0)
        self.assertEqual(self.mod.heartbeat_interval_s({"heartbeat_interval_s": "30"}), 30.0)
        self.assertEqual(self.mod.heartbeat_interval_s({"heartbeat_interval_s": "junk"}), 15.0)


class UnixSocketOverride(unittest.TestCase):
    """The one seam that keeps a test run off the HOST's real LXD.

    The repair path WRITES `core.https_address` through the daemon's unix socket. A
    suite run on a machine that happens to run LXD rewrote that machine's config
    once (t_0139006c); ``TERMINI_LXD_UNIX_SOCKETS`` makes that impossible for any
    test that starts the daemon as a subprocess and cannot patch the module.
    """

    def test_env_override_replaces_the_real_socket_list(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bogus = [str(pathlib.Path(tmp.name) / "a.socket"), str(pathlib.Path(tmp.name) / "b.socket")]
        with mock.patch.dict(os.environ, {"TERMINI_LXD_UNIX_SOCKETS": ":".join(bogus)}):
            mod = load_daemon({})
        self.assertEqual(mod.LXD_UNIX_SOCKETS, tuple(bogus))

    def test_unset_or_empty_env_keeps_the_documented_sockets(self):
        with mock.patch.dict(os.environ, {"TERMINI_LXD_UNIX_SOCKETS": ""}):
            mod = load_daemon({})
        self.assertEqual(mod.LXD_UNIX_SOCKETS[0], "/var/snap/lxd/common/lxd/unix.socket")


if __name__ == "__main__":
    unittest.main()
