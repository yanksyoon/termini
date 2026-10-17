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


if __name__ == "__main__":
    unittest.main()
