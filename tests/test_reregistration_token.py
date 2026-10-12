"""Re-registration must persist the node token it hands out.

Regression cover for a real bug found while resuming the LIVE demo — not by reading
code, but because the seeder could not start:

    [seed] telemetry rejected for 01M4NZK46...: 403 PERMISSION_DENIED: invalid node token

`upsert_node` has two paths. The INSERT path stored `node_token_hash`; the UPDATE
path (taken when a node registers AGAIN) stored everything EXCEPT the token hash and
`status`. But the controller mints a FRESH token on every registration and returns it
to the node — so on any re-registration the node received a token whose hash was never
persisted, and every subsequent request was 403'd. A re-registered node also kept
whatever `status` it had, so it stayed OFFLINE on the dashboard.

That is why the repo's `infra/cloudrun/setup-seeder-vm.sh` "resumes" by DELETING
/data/controller.db: it forces the INSERT path, at the cost of the entire accumulated
audit and decision history. With this fixed, the demo can be restarted against its
existing database.

Both properties are asserted below, because either alone still breaks a restart.
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER = REPO / "controller"
sys.path.insert(0, str(CONTROLLER))
sys.path.insert(0, str(REPO))

from app import db as appdb   # noqa: E402
from app import store         # noqa: E402

PROJECT_ID = "01J0DEM0WARM000000000000AB"


def _reg(machine_id: str = "demo-node-a") -> dict:
    return {
        "registration_id": machine_id,
        "machine_id": machine_id,
        "hostname": "a" * 16,
        "hardware_json": {"cpu": {"cores": 2, "threads": 4}},
        "os": {"name": "ubuntu", "version": "24.04"},
        "lxd": {"version": "5.21.8", "snap_revision": "40958"},
        "installer_version": "0.1.0",
        "role_hint": "compute",
    }


class ReregistrationPersistsToken(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = pathlib.Path(self.tmp.name) / "controller.db"
        appdb.migrate(str(self.db))
        self.conn = sqlite3.connect(str(self.db))
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_first_registration_creates_and_authenticates(self):
        nid, created = store.upsert_node(self.conn, PROJECT_ID, _reg(), "compute",
                                         store.sha256("tok-1"), None, store.now_iso())
        self.assertTrue(created)
        self.assertIsNotNone(store.get_node_by_token_hash(self.conn, store.sha256("tok-1")))

    def test_second_registration_token_authenticates(self):
        """The bug: the token handed out on re-registration was never persisted."""
        nid1, created1 = store.upsert_node(self.conn, PROJECT_ID, _reg(), "compute",
                                           store.sha256("tok-1"), None, store.now_iso())
        self.assertTrue(created1)
        # simulate the node having gone offline before it came back
        self.conn.execute("UPDATE nodes SET status='offline' WHERE id=?", (nid1,))
        self.conn.commit()

        nid2, created2 = store.upsert_node(self.conn, PROJECT_ID, _reg(), "compute",
                                           store.sha256("tok-2"), None, store.now_iso())
        self.assertFalse(created2, "re-registration must reuse the existing node row")
        self.assertEqual(nid1, nid2, "re-registration must keep the same node identity")
        self.assertIsNotNone(
            store.get_node_by_token_hash(self.conn, store.sha256("tok-2")),
            "the token the controller just handed out MUST authenticate")

    def test_reregistration_marks_the_node_online(self):
        nid, _ = store.upsert_node(self.conn, PROJECT_ID, _reg(), "compute",
                                   store.sha256("tok-1"), None, store.now_iso())
        self.conn.execute("UPDATE nodes SET status='offline' WHERE id=?", (nid,))
        self.conn.commit()
        store.upsert_node(self.conn, PROJECT_ID, _reg(), "compute",
                          store.sha256("tok-2"), None, store.now_iso())
        self.assertEqual(store.get_node(self.conn, nid)["status"], "online",
                         "a node that just registered is ONLINE")

    def test_old_token_stops_working_after_rotation(self):
        """Rotation must actually replace, not merely add."""
        store.upsert_node(self.conn, PROJECT_ID, _reg(), "compute",
                          store.sha256("tok-1"), None, store.now_iso())
        store.upsert_node(self.conn, PROJECT_ID, _reg(), "compute",
                          store.sha256("tok-2"), None, store.now_iso())
        self.assertIsNone(store.get_node_by_token_hash(self.conn, store.sha256("tok-1")),
                          "the superseded token must no longer authenticate")


if __name__ == "__main__":
    unittest.main()
