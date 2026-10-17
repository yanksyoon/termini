"""Day-0 leaves a node the operator can ACT on — LXD trust installed at registration.

MEASURED DEFECT (live, 2026-10-17). A node booted from the pre-baked image family
registered in 245 ms, streamed telemetry, and appeared `online` on the dashboard —
and the operator's first remediation against it answered

    HTTP 403: {"error_code":403,"error":"Forbidden"}
    PUT /1.0/instances/<name>/state

because LXD authorises per CLIENT CERTIFICATE and trust was installed only at
CONTROLLER STARTUP, for a static `TERMINI_LXD_TRUST_NODES` list. A node enrolled
later was authenticated TO the controller and not authorised BY it. The same 403
also broke the vm_id -> instance-name resolution (it needs `GET /1.0/instances`),
which is what made the executor address the instance by its derived ULID.

The fix pinned here: the node mints its own one-time certificate-add trust token
(only the node's host can) and pushes it to `POST /v1/nodes/{id}/lxd-trust` with
its own node token; the controller installs its client certificate into that
node's trust store and reports the outcome.

Security properties pinned by this test:
  * node token only, own node only (`_require_node`);
  * the endpoint must be the host:port the node registered with — a node may not
    redirect the controller's trust installation at another host;
  * a trust token that the node's LXD rejects leaves `trusted: false`, it does not
    500, and it does not 200 as if it worked.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))
sys.path.insert(0, str(REPO / "tests"))

from app import config, db, events, inventory, logs, server as server_mod, sshca, store, terminal  # noqa: E402
from app import lxdtrust  # noqa: E402
from test_lxdtrust import _StubHarness  # noqa: E402  (reuses the mutual-TLS LXD stub)

GiB = 1024 ** 3
PROJECT_ID = "01J0DEM0WARM000000000000AB"
PROJECT_TOKEN = "test-project-token"
NODE_TOKEN = "node-token-value"
TRUST_SECRET = "trust-tok-1"


def _load_cfg(db_path: str, cert: str, key: str):
    env = {
        "TERMINI_BOOTSTRAP_TOKEN": "boot-a",
        "TERMINI_PROJECT_TOKEN": PROJECT_TOKEN,
        "TERMINI_PROJECT_ID": PROJECT_ID,
        "TERMINI_DB_PATH": db_path,
        "TERMINI_MAX_INFERENCE_NODES": "1",
        "TERMINI_LXD_BASE_URL": "http://127.0.0.1:1",
        "TERMINI_LXD_CLIENT_CERT": cert,
        "TERMINI_LXD_CLIENT_KEY": key,
        "TERMINI_HOST": "127.0.0.1",
        "TERMINI_PORT": "0",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        return config.Config.load()


def _registration(endpoint: str) -> dict:
    return {
        "shape": "canonical",
        "synthetic": False,
        "registration_id": "node-d",
        "machine_id": "machine-d",
        "hostname": "node-d",
        "role_hint": "compute",
        "cpu_cores": 2,
        "ram_bytes": 8 * GiB,
        "disk_bytes": 60 * GiB,
        "gpu_vram_bytes": 0,
        "gpu_count": 0,
        "os": None,
        "lxd": None,
        "lxd_endpoint": endpoint,
        "installer_version": "2026.10.0",
        "hardware_json": {"cpu": {"cores": 2},
                          "memory": {"total_bytes": 8 * GiB},
                          "gpus": [], "disks": []},
    }


class NodeLxdTrustAtRegistrationTest(_StubHarness):
    """The controller installs its cert on a node that enrolled after startup."""

    def setUp(self):
        super().setUp()
        self.db_path = str(self.dir / "controller.db")
        db.migrate(self.db_path)
        self.identity = self._identity("controller")           # the controller's client cert
        self.stub = self._stub(self.identity.cert_path, TRUST_SECRET)
        self.cfg = _load_cfg(self.db_path, self.identity.cert_path, self.identity.key_path)

        with db.session(self.db_path) as conn:
            store.seed_bootstrap_tokens(conn, list(self.cfg.bootstrap_tokens), store.now_iso())
            self.project, _ = store.ensure_project(conn, self.cfg, store.now_iso())
            # Registered AFTER the controller started: exactly the live case.
            self.node_id, _created = store.upsert_node(
                conn, self.project["id"], _registration(self.stub.base_url), "compute",
                store.sha256(NODE_TOKEN), "10.10.0.14", store.now_iso(),
            )

        self.broker = events.EventBroker()
        self.logs = logs.OperatorLogs(self.broker, ring_size=100)
        self.srv = server_mod.ControllerServer(
            ("127.0.0.1", 0), self.cfg, self.project, self.broker, self.logs,
            inventory.Inventory.load(None, "ubuntu", 22), terminal.TerminalManager(),
            sshca.SSHCA(None),
        )
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    # ---------------------------------------------------------------- plumbing
    def _post_trust(self, body, *, node_token: str | None = NODE_TOKEN, node_id: str | None = None):
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/nodes/{node_id or self.node_id}/lxd-trust",
            data=data,
            headers={"Authorization": "Bearer " + (node_token or ""),
                     "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            finally:
                e.close()

    def _cert_posts(self) -> int:
        return sum(1 for m, path, _b in self.stub.requests if m == "POST" and path == "/1.0/certificates")

    # ------------------------------------------------------------------- tests
    def test_token_installs_the_controller_cert(self):
        status, body = self._post_trust(
            {"lxd": {"endpoint": self.stub.base_url}, "trust_token": TRUST_SECRET})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["trusted"])
        self.assertEqual(body["status"], "trusted")
        self.assertEqual(self._cert_posts(), 1)
        # and the point of it: the controller can now LIST the node's instances
        after = lxdtrust.probe_trusted(self.stub.base_url, self.identity)
        self.assertTrue(after.trusted, after.detail)

    def test_second_call_is_idempotent_and_does_not_consume_a_second_token(self):
        self._post_trust({"lxd": {"endpoint": self.stub.base_url}, "trust_token": TRUST_SECRET})
        status, body = self._post_trust(
            {"lxd": {"endpoint": self.stub.base_url}, "trust_token": "another-token"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["trusted"])
        self.assertEqual(body["status"], "already_trusted")
        self.assertEqual(self._cert_posts(), 1)

    def test_a_rejected_token_reports_untrusted_and_does_not_lie(self):
        status, body = self._post_trust(
            {"lxd": {"endpoint": self.stub.base_url}, "trust_token": "wrong-token"})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["trusted"])
        self.assertEqual(body["status"], "error")
        self.assertIn("403", body["detail"])
        self.assertFalse(lxdtrust.probe_trusted(self.stub.base_url, self.identity).trusted)

    def test_endpoint_must_be_the_node_s_own(self):
        status, body = self._post_trust(
            {"lxd": {"endpoint": "10.10.0.99:8443"}, "trust_token": TRUST_SECRET})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "PERMISSION_DENIED")
        self.assertEqual(self._cert_posts(), 0)

    def test_another_node_token_is_refused(self):
        status, body = self._post_trust(
            {"lxd": {"endpoint": self.stub.base_url}, "trust_token": TRUST_SECRET},
            node_token="some-other-token")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "PERMISSION_DENIED")
        self.assertEqual(self._cert_posts(), 0)

    def test_missing_secret_is_a_422_not_a_silent_noop(self):
        status, body = self._post_trust({"lxd": {"endpoint": self.stub.base_url}})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(self._cert_posts(), 0)


if __name__ == "__main__":
    unittest.main()
