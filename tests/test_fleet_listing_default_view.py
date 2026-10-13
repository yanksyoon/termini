"""DEFECT 2 regression — the default fleet listing must be the REAL fleet.

MEASURED DEFECT (live standing fleet, 2026-10-13). ``GET /v1/projects/{pid}/nodes``
returned 8 rows / 6 offline while the deck shows two nodes: the 2 real registered
nodes, 2 seeder-fabricated rows (``demo-node-a`` / ``demo-node-c``), and 4 stale
phantom rows from earlier test runs. The dashboard renders whatever the API
returns, so it contradicted the deck.

Two mechanisms fix it:

  * the decommission route (``#134``) retires the stale phantom rows out of the
    listing (covered in test_allocation_release.py);
  * the default listing applies the same provenance rule as placement
    (``_selection_pool``): a ``synthetic`` row is excluded while any real node is
    registered, so a stopped seeder's stale rows cannot re-introduce the
    contradiction. A seeder-only demo (no real node) still lists its fabricated
    rows, and ``?include=synthetic`` returns everything (provenance intact).
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

from app import config, db, events, inventory, logs, server as server_mod, sshca, store, terminal, ulid  # noqa: E402

GiB = 1024 ** 3
PROJECT_ID = "01J0DEM0WARM000000000000AB"
PROJECT_TOKEN = "test-project-token"

COMPUTE_HW = {
    "cpu": {"cores": 8, "threads": 8},
    "memory": {"total_bytes": 32 * GiB},
    "gpus": [],
    "disks": [{"size_bytes": 256 * GiB}],
}


def _load_cfg(db_path: str):
    env = {
        "TERMINI_BOOTSTRAP_TOKEN": "boot-a,boot-c",
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


class FleetListingDefaultViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-fleet-listing-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = os.path.join(self.tmp, "controller.db")
        db.migrate(self.db_path)
        self.cfg = _load_cfg(self.db_path)
        with db.session(self.db_path) as conn:
            store.seed_bootstrap_tokens(conn, list(self.cfg.bootstrap_tokens), store.now_iso())
            self.project, _ = store.ensure_project(conn, self.cfg, store.now_iso())

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
    def _get(self, path):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers={"Authorization": "Bearer " + PROJECT_TOKEN}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            finally:
                e.close()

    def _post(self, path, body=None):
        data = json.dumps(body if body is not None else {}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + PROJECT_TOKEN}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            finally:
                e.close()

    def _add_node(self, reg_id, *, synthetic=False, role="compute"):
        with db.session(self.db_path) as conn:
            reg = {
                "registration_id": reg_id, "machine_id": reg_id, "hostname": reg_id,
                "role_hint": None, "installer_version": None,
                "hardware_json": COMPUTE_HW, "os": None, "lxd": None,
                "lxd_endpoint": None, "synthetic": synthetic,
            }
            node_id, _ = store.upsert_node(
                conn, PROJECT_ID, reg, role, store.sha256(reg_id), None, store.now_iso())
        return node_id

    def _listing(self, path="/v1/projects/%s/nodes" % PROJECT_ID):
        st, body = self._get(path)
        self.assertEqual(st, 200, body)
        return body["nodes"]

    # -------------------------------------------------------------------- tests
    def test_default_listing_is_the_real_fleet_when_a_real_node_exists(self):
        real = self._add_node("ba1f1faeacb4501aed12cf8ea1e5713b")
        self._add_node("demo-node-a", synthetic=True)
        self._add_node("demo-node-c", synthetic=True, role="inference")

        ids = [n["node_id"] for n in self._listing()]
        self.assertEqual(ids, [real])

    def test_include_synthetic_returns_every_row_with_provenance(self):
        real = self._add_node("ba1f1faeacb4501aed12cf8ea1e5713b")
        synth = self._add_node("demo-node-a", synthetic=True)

        nodes = self._listing(
            "/v1/projects/%s/nodes?include=synthetic" % PROJECT_ID)
        by_id = {n["node_id"]: n for n in nodes}
        self.assertEqual(set(by_id), {real, synth})
        self.assertFalse(by_id[real]["synthetic"])
        self.assertTrue(by_id[synth]["synthetic"])

    def test_a_seeder_only_demo_still_lists_its_fabricated_rows(self):
        a = self._add_node("demo-node-a", synthetic=True)
        c = self._add_node("demo-node-c", synthetic=True, role="inference")

        ids = {n["node_id"] for n in self._listing()}
        self.assertEqual(ids, {a, c})

    def test_decommissioned_node_leaves_the_listing(self):
        real = self._add_node("ba1f1faeacb4501aed12cf8ea1e5713b")
        synth = self._add_node("demo-node-a", synthetic=True)

        st, body = self._post(
            "/v1/projects/%s/nodes/%s/decommission" % (PROJECT_ID, real), {})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["status"], "decommissioned")

        # The retired real node is gone; with no real node left the (still
        # registered) synthetic row is what a seeder-only demo would show.
        ids = {n["node_id"] for n in self._listing()}
        self.assertNotIn(real, ids)
        self.assertEqual(ids, {synth})


if __name__ == "__main__":
    unittest.main(verbosity=2)
