"""Node side of "day-0 leaves a node the operator can ACT on".

`install_lxd_trust` mints a one-time certificate-add trust token on the node and
pushes it to `POST /v1/nodes/{id}/lxd-trust` with the node's OWN token, so the
controller can put its client certificate into that node's LXD trust store at
registration time instead of at some later controller restart.

Unit-level only (`lxc config trust add` needs a live LXD), so the subprocess, the
LXD API probe and the HTTP client are faked. What is pinned here:
  * the request goes to `{controller}/v1/nodes/{node_id}/lxd-trust` with
    `Authorization: Bearer <node_token>` and carries `lxd.endpoint`;
  * `trusted: true` is reported as installed, `trusted: false` as not installed;
  * a node that cannot mint a token, or a controller that refuses, never raises —
    trust is the controller's problem to repair and must never stop telemetry.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
DAEMON = REPO / "infra" / "bootstrap" / "node" / "termini-bootstrap.py"


def load_daemon():
    tmp = tempfile.mkdtemp(prefix="termini-trust-push-")
    os.environ.setdefault("TERMINI_STATE_DIR", tmp)
    spec = importlib.util.spec_from_file_location("termini_bootstrap_trust_push", DAEMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


CFG = {"controller_url": "http://10.10.0.2:8080", "node_name": "node-d"}
IDENTITY = {"node_id": "01NODE", "node_token": "node-tok", "role": "compute"}


class InstallLxdTrustTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_daemon()
        self.calls: list[tuple] = []
        self.mod.ensure_lxd_api = lambda cfg: {"ok": True, "endpoint": "https://10.10.0.14:8443",
                                               "detail": "", "repaired": False}
        self.mod.mint_lxd_trust_token = lambda name="termini-controller": "trust-tok-1"
        # Deterministic CLI-fallback tests: this host HAS a real LXD socket
        # (/var/snap/lxd/common/lxd/unix.socket), and a test must never mint a
        # certificate-add token against the machine running it. The socket path
        # has its own tests against a stub (`SocketMintedTrustTokenTest`).
        self.mod.mint_trust_token_over_socket = lambda *a, **k: None
        self.mod.log = lambda msg: None

    def _http(self, reply_status, reply_body):
        def fake(method, url, body=None, token=None, timeout=None):
            self.calls.append((method, url, body, token))
            return reply_status, reply_body
        return fake

    def test_pushes_token_to_own_node_route(self):
        self.mod.http_json = self._http(200, {"trusted": True, "status": "trusted",
                                              "endpoint": "https://10.10.0.14:8443",
                                              "detail": "certificate added"})
        ok = self.mod.install_lxd_trust(CFG, IDENTITY)
        self.assertTrue(ok)
        method, url, body, token = self.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "http://10.10.0.2:8080/v1/nodes/01NODE/lxd-trust")
        self.assertEqual(token, "node-tok")
        self.assertEqual(body["trust_token"], "trust-tok-1")
        self.assertEqual(body["lxd"]["endpoint"], "https://10.10.0.14:8443")

    def test_untrusted_answer_is_reported_not_raised(self):
        self.mod.http_json = self._http(200, {"trusted": False, "status": "error",
                                              "detail": "trust install failed: HTTP 403"})
        self.assertFalse(self.mod.install_lxd_trust(CFG, IDENTITY))

    def test_controller_error_is_swallowed(self):
        self.mod.http_json = self._http(503, {"error": {"code": "UNAVAILABLE"}})
        self.assertFalse(self.mod.install_lxd_trust(CFG, IDENTITY))

    def test_transport_failure_is_swallowed(self):
        def boom(*a, **k):
            raise OSError("network down")
        self.mod.http_json = boom
        self.assertFalse(self.mod.install_lxd_trust(CFG, IDENTITY))

    def test_no_token_minted_means_no_request(self):
        self.mod.mint_lxd_trust_token = lambda name="termini-controller": None
        self.mod.http_json = self._http(200, {"trusted": True})
        self.assertFalse(self.mod.install_lxd_trust(CFG, IDENTITY))
        self.assertEqual(self.calls, [])

    def test_dead_lxd_api_means_no_request(self):
        self.mod.ensure_lxd_api = lambda cfg: {"ok": False, "endpoint": "", "detail": "refused",
                                               "repaired": False}
        self.mod.http_json = self._http(200, {"trusted": True})
        self.assertFalse(self.mod.install_lxd_trust(CFG, IDENTITY))
        self.assertEqual(self.calls, [])


# --------------------------------------------------------------------------- socket transport
# On a deployed node the snap `lxc` CLI cannot exec under the unit's
# `NoNewPrivileges=true` ("cannot change profile for the next exec call: Operation
# not permitted"), so the token MUST be minted over LXD's unix socket. These tests
# drive the real transport against a stub LXD on a unix socket and pin both the
# wire shape and the exact token encoding `lxc config trust add` produces.
import base64
import json
import socketserver
import tempfile
import threading
from http.server import BaseHTTPRequestHandler


def load_daemon_with_socket(sock_path: str):
    os.environ["TERMINI_LXD_UNIX_SOCKETS"] = sock_path
    return load_daemon()


class _StubLxdHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "_StubLxdUnix"

    def log_message(self, *args):  # silence
        return

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0) or 0)
        payload = json.loads(self.rfile.read(n) or b"{}")
        self.server.requests.append(("POST", self.path, payload))
        raw = json.dumps(self.server.reply).encode()
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class _StubLxdUnix(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path, *, status=200, reply=None):
        self.status = status
        self.reply = reply if reply is not None else {
            "type": "sync",
            "metadata": {
                "client_name": "termini-controller",
                "addresses": ["10.10.0.14:8443"],
                "secret": "s3cr3t-value",
                "expires_at": "2026-10-18T00:00:00Z",
            },
        }
        self.requests = []
        super().__init__(path, _StubLxdHandler)


class SocketMintedTrustTokenTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="termini-lxd-sock-")
        self.sock_path = os.path.join(self.dir, "unix.socket")
        self.srv = _StubLxdUnix(self.sock_path)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.mod = load_daemon_with_socket(self.sock_path)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        os.environ.pop("TERMINI_LXD_UNIX_SOCKETS", None)

    def test_mints_the_same_token_shape_the_cli_prints(self):
        token = self.mod.mint_trust_token_over_socket("termini-controller")
        self.assertIsNotNone(token)
        decoded = json.loads(base64.b64decode(token))
        self.assertEqual(decoded["client_name"], "termini-controller")
        self.assertEqual(decoded["addresses"], ["10.10.0.14:8443"])
        self.assertEqual(decoded["secret"], "s3cr3t-value")
        self.assertEqual(decoded["expires_at"], "2026-10-18T00:00:00Z")
        method, path, body = self.srv.requests[0]
        self.assertEqual((method, path), ("POST", "/1.0/certificates"))
        self.assertEqual(body, {"type": "client", "name": "termini-controller", "token": True})

    def test_token_is_recognised_by_the_daemons_own_extractor(self):
        token = self.mod.mint_trust_token_over_socket()
        self.assertEqual(self.mod.extract_trust_token(token), token)

    def test_error_status_is_not_a_token(self):
        self.srv.status = 500
        self.assertIsNone(self.mod.mint_trust_token_over_socket())

    def test_missing_secret_is_not_a_token(self):
        self.srv.reply = {"type": "sync", "metadata": {"addresses": []}}
        self.assertIsNone(self.mod.mint_trust_token_over_socket())

    def test_install_prefers_the_socket_and_never_shells_out(self):
        calls = []
        self.mod.mint_lxd_trust_token = lambda name="termini-controller": calls.append(name) or "cli-token"
        self.mod.ensure_lxd_api = lambda cfg: {"ok": True, "endpoint": "https://10.10.0.14:8443",
                                               "detail": "", "repaired": False}
        sent = {}

        def fake_http(method, url, body=None, token=None, timeout=None):
            sent.update(body or {})
            return 200, {"trusted": True, "status": "trusted", "endpoint": "x", "detail": "d"}

        self.mod.http_json = fake_http
        self.assertTrue(self.mod.install_lxd_trust(CFG, IDENTITY))
        self.assertEqual(calls, [], "the CLI must not be used when the socket works")
        self.assertIsNotNone(sent.get("trust_token"))
        decoded = json.loads(base64.b64decode(sent["trust_token"]))
        self.assertEqual(decoded["secret"], "s3cr3t-value")


if __name__ == "__main__":
    unittest.main()
