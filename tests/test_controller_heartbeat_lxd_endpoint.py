"""Heartbeat is how a node keeps its OWN LXD endpoint current.

MEASURED DEFECT (live, 2026-10-17). BOTH real nodes reported `lxd_endpoint = None`
in `nodes`, so `operator/loop.py` resolved every remediation through the single
global `TERMINI_LXD_BASE_URL=https://10.10.0.11:8443`. Registration is one-shot:
a node registered before the field existed (or before its listener came up) could
never correct it, and every node's action dialled node-a.

The fix pinned here: the heartbeat accepts the same endpoint field as registration
(`lxd_endpoint` flat, or `lxd.endpoint` nested), validates it identically
(a malformed value is a 422, never a silent fallback), and records it so the
per-node executor resolver never falls back for a node that has one. An old
heartbeat with no body stays valid; a heartbeat that says the listener is down
records no endpoint (the node only advertises a proven listener).
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

from app import config, db, events, inventory, logs, server as server_mod, sshca, store, terminal, ulid  # noqa: E402

GiB = 1024 ** 3
PROJECT_ID = "01J0DEM0WARM000000000000AB"
PROJECT_TOKEN = "test-project-token"
NODE_TOKEN = "node-token-value"


def _load_cfg(db_path: str):
    env = {
        "TERMINI_BOOTSTRAP_TOKEN": "boot-a",
        "TERMINI_PROJECT_TOKEN": PROJECT_TOKEN,
        "TERMINI_PROJECT_ID": PROJECT_ID,
        "TERMINI_DB_PATH": db_path,
        "TERMINI_MAX_INFERENCE_NODES": "1",
        "TERMINI_LXD_BASE_URL": "http://127.0.0.1:1",
        "TERMINI_HOST": "127.0.0.1",
        "TERMINI_PORT": "0",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        return config.Config.load()


def _registration(endpoint: str | None) -> dict:
    reg = {
        "shape": "canonical",
        "synthetic": False,
        "registration_id": "node-a",
        "machine_id": "machine-1",
        "hostname": "node-a",
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
        "hardware_json": {"cpu": {"cores": 2}, "memory": {"total_bytes": 8 * GiB},
                          "gpus": [], "disks": []},
    }
    return reg


class HeartbeatEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-heartbeat-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = os.path.join(self.tmp, "controller.db")
        db.migrate(self.db_path)
        self.cfg = _load_cfg(self.db_path)
        with db.session(self.db_path) as conn:
            store.seed_bootstrap_tokens(conn, list(self.cfg.bootstrap_tokens), store.now_iso())
            self.project, _ = store.ensure_project(conn, self.cfg, store.now_iso())
            # A node registered BEFORE it reported an endpoint — the exact state
            # both real nodes were in.
            self.node_id, _created = store.upsert_node(
                conn, self.project["id"], _registration(None), "compute",
                store.sha256(NODE_TOKEN), "10.10.0.11", store.now_iso(),
            )

        self.broker = events.EventBroker()
        self.logs = logs.OperatorLogs(self.broker, ring_size=100)
        self.srv = server_mod.ControllerServer(
            ("127.0.0.1", 0), self.cfg, self.project, self.broker, self.logs,
            inventory.Inventory.load(None, "ubuntu", 22), terminal.TerminalManager(),
            sshca.SSHCA(None),
        )
        self.port = self.srv.server_address[1]
        self._thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self._thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    # ---------------------------------------------------------------- plumbing
    def _heartbeat(self, body=None, *, node_token: str | None = NODE_TOKEN):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Authorization": "Bearer " + (node_token or "")}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/nodes/{self.node_id}/heartbeat",
            data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            finally:
                e.close()

    def stored_endpoint(self):
        with db.session(self.db_path) as conn:
            return store.get_node(conn, self.node_id)["lxd_endpoint"]

    # ------------------------------------------------------------------ tests
    def test_flat_endpoint_is_recorded(self):
        status, _ = self._heartbeat({"lxd_endpoint": "10.10.0.11:8443"})
        self.assertIn(status, (200, 204))
        self.assertEqual(self.stored_endpoint(), "10.10.0.11:8443")

    def test_nested_endpoint_is_accepted_too(self):
        status, _ = self._heartbeat({"lxd": {"endpoint": "10.10.0.13:8443"}})
        self.assertIn(status, (200, 204))
        self.assertEqual(self.stored_endpoint(), "10.10.0.13:8443")

    def test_later_address_replaces_the_stored_one(self):
        self._heartbeat({"lxd_endpoint": "10.10.0.11:8443"})
        self._heartbeat({"lxd_endpoint": "10.10.0.21:8443"})
        self.assertEqual(self.stored_endpoint(), "10.10.0.21:8443")

    def test_malformed_endpoint_is_refused_not_silently_dropped(self):
        status, body = self._heartbeat({"lxd_endpoint": "not-an-endpoint"})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "INVALID_ARGUMENT")
        self.assertIsNone(self.stored_endpoint())

    def test_old_empty_heartbeat_still_works(self):
        status, _ = self._heartbeat()
        self.assertIn(status, (200, 204))
        self.assertIsNone(self.stored_endpoint())

    def test_dead_listener_reports_no_endpoint(self):
        """A node whose LXD is down advertises nothing: no endpoint is written."""
        status, _ = self._heartbeat({
            "lxd_api": {"listening": False, "endpoint": "10.10.0.11:8443",
                        "detail": "ConnectionRefusedError: [Errno 111]"},
        })
        self.assertIn(status, (200, 204))
        self.assertIsNone(self.stored_endpoint())

    def test_unknown_node_token_is_refused(self):
        status, body = self._heartbeat({"lxd_endpoint": "10.10.0.11:8443"},
                                      node_token="wrong-token")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "PERMISSION_DENIED")
        self.assertIsNone(self.stored_endpoint())


if __name__ == "__main__":
    unittest.main()
