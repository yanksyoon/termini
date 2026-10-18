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
    """The node adds the controller's published certificate to its OWN trust store."""

    PEM = ("-----BEGIN CERTIFICATE-----\n"
           "MIIBsTCCAVegAwIBAgIUHleGhZRLaikvRZY4e/tPVU271k8wCgYIKoZIzj0EAwIw\n"
           "HTEbMBkGA1UEAwwSdGVybWluaS1jb250cm9sbGVyMB4XDTI2MTAxMjE3MjUxNloX\n"
           "-----END CERTIFICATE-----\n")

    def setUp(self):
        self.mod = load_daemon()
        self.calls: list[tuple] = []
        self.config_sets: list[tuple[str, str]] = []
        self.added: list[str] = []
        self.mod.ensure_lxd_api = lambda cfg: {"ok": True, "endpoint": "https://10.10.0.14:8443",
                                               "detail": "", "repaired": False}
        # never dial the network from a unit test
        self.mod._wait_for_own_lxd_listener = lambda endpoint, timeout_s=90: True
        self.mod._add_certificate_over_socket = lambda cert_b64, name="termini-controller": (
            self.added.append(cert_b64) or (True, "stub: HTTP 201"))
        self.mod.log = lambda msg: None

    def _http(self, reply_status, reply_body, *, cert_status=200, cert_pem=None):
        pem = self.PEM if cert_pem is None else cert_pem

        def fake(method, url, body=None, token=None, timeout=None):
            self.calls.append((method, url, body, token))
            if method == "GET":
                return cert_status, {"certificate_pem": pem, "fingerprint": "abc"}
            return reply_status, reply_body

        return fake

    def test_fetches_the_certificate_and_adds_it_locally(self):
        self.mod.http_json = self._http(200, {"trusted": True, "status": "trusted",
                                              "endpoint": "https://10.10.0.14:8443",
                                              "detail": "certificate added"})
        self.assertTrue(self.mod.install_lxd_trust(CFG, IDENTITY))
        get_call, post_call = self.calls[0], self.calls[1]
        self.assertEqual(get_call[0], "GET")
        self.assertEqual(get_call[1], "http://10.10.0.2:8080/v1/nodes/01NODE/lxd-trust-cert")
        self.assertEqual(get_call[3], "node-tok")
        # the certificate handed to LXD is the PEM BODY (base64 of the DER), no armor
        self.assertEqual(len(self.added), 1)
        self.assertNotIn("BEGIN CERTIFICATE", self.added[0])
        self.assertNotIn("\n", self.added[0])
        self.assertTrue(self.added[0].startswith("MIIBsTCCAVeg"))
        # and the confirm call carries the endpoint, not a secret
        self.assertEqual(post_call[0], "POST")
        self.assertTrue(post_call[2]["certificate_added"])
        self.assertEqual(post_call[2]["lxd"]["endpoint"], "https://10.10.0.14:8443")
        self.assertNotIn("trust_password", post_call[2])

    def test_controller_refusing_the_certificate_means_no_add(self):
        self.mod.http_json = self._http(200, {"trusted": True}, cert_status=500)
        self.assertFalse(self.mod.install_lxd_trust(CFG, IDENTITY))
        self.assertEqual(self.added, [])

    def test_a_body_that_is_not_a_certificate_is_refused(self):
        self.mod.http_json = self._http(200, {"trusted": True}, cert_pem="not a certificate")
        self.assertFalse(self.mod.install_lxd_trust(CFG, IDENTITY))
        self.assertEqual(self.added, [])

    def test_lxd_rejecting_the_add_means_no_confirm(self):
        self.mod._add_certificate_over_socket = lambda *a, **k: (False, "HTTP 400: bad cert")
        self.mod.http_json = self._http(200, {"trusted": True})
        self.assertFalse(self.mod.install_lxd_trust(CFG, IDENTITY))
        self.assertEqual([c[0] for c in self.calls], ["GET"], "no confirm call after a failed add")

    def test_untrusted_probe_answer_is_reported_not_raised(self):
        self.mod.http_json = self._http(200, {"trusted": False, "status": "error",
                                              "detail": "GET /1.0/instances -> HTTP 403"})
        self.assertFalse(self.mod.install_lxd_trust(CFG, IDENTITY))
        self.assertEqual(len(self.added), 1)

    def test_transport_failure_is_swallowed(self):
        def boom(*a, **k):
            raise OSError("network down")
        self.mod._wait_for_own_lxd_listener = lambda endpoint, timeout_s=90: True
        self.mod.http_json = boom
        self.assertFalse(self.mod.install_lxd_trust(CFG, IDENTITY))

    def test_listener_that_never_accepts_means_no_request(self):
        self.mod._wait_for_own_lxd_listener = lambda endpoint, timeout_s=90: False
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
        # never dial the network from a unit test
        self.mod._wait_for_own_lxd_listener = lambda endpoint, timeout_s=90: True

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

    def test_add_certificate_over_the_socket_uses_the_real_transport(self):
        """The cert is added with a POST to LXD's socket — never the `lxc` CLI,
        which cannot exec under the unit's NoNewPrivileges."""
        self.srv.status = 201
        self.srv.reply = {"type": "sync", "metadata": {}}
        ok, detail = self.mod._add_certificate_over_socket("MIIBsTCCAVeg", "termini-controller")
        self.assertTrue(ok, detail)
        method, path, body = self.srv.requests[0]
        self.assertEqual((method, path), ("POST", "/1.0/certificates"))
        self.assertEqual(body["certificate"], "MIIBsTCCAVeg")
        self.assertEqual(body["type"], "client")
        self.assertEqual(body["name"], "termini-controller")

    def test_socket_add_reports_a_rejection(self):
        self.srv.status = 400
        self.srv.reply = {"type": "error", "error": "x509: malformed certificate"}
        ok, detail = self.mod._add_certificate_over_socket("not-a-cert")
        self.assertFalse(ok)
        self.assertIn("400", detail)


if __name__ == "__main__":
    unittest.main()
