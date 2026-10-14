"""Startup self-heal for never-applied reservations orphaned by a restart.

`POST /intents` commits capacity when it ACCEPTS a plan (controller-api.md §5.2
step 7), and an accepted plan lives only in the controller's in-memory `plans`
dict. `POST /vms/reconcile` cannot release such a reservation: it proves absence
from the node, and a commit with no recorded instances has no absence to prove.
So a plan submitted and never applied used to shrink a node's free capacity
permanently — live, node-a carried a never-applied 2 vCPU / 2 GiB / 32 GiB
reservation from an earlier session, its `ram_free_gb` was 1, and *any*
two-agent deploy (even at the 1 GiB minimum) was refused
`exceeds_node_ram_ceiling` (Defect B).

The released capacity is proven dead, not assumed: the accepted-plan map never
leaves the process, so after a restart the reservation can never become an
instance (the client's `POST /vms/apply` would `404 plan not found`).

These tests cover the three cases the sweep must tell apart, and the end-to-end
effect on the natural-language deploy.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER = REPO / "controller"
sys.path.insert(0, str(CONTROLLER))
sys.path.insert(0, str(REPO))

from app import db, health, server as server_mod, store  # noqa: E402
from termini.intent import DEFAULT_AGENT_VM, DEFAULT_HOST_CAPS, clamp_validate  # noqa: E402

PROJECT_ID = "01J0DEM0WARM000000000000AE"

# node-a's real shape (2 vCPU / 7 GiB registered -> 4 vCPU, 5 GiB allocatable).
NODE_A_HW = {
    "cpu": {"cores": 2, "threads": 2},
    "memory": {"total_bytes": 8319729664},
    "gpus": [],
    "disks": [{"size_bytes": 61285326848}],
}


class _Handler:
    def _resolve_target(self, spec, nodes):
        return server_mod.ControllerHandler._resolve_target(None, spec, nodes)


class OrphanedReservationTest(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        db.migrate(self.path)
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "INSERT INTO projects (id, name, project_token_hash, max_inference_nodes, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (PROJECT_ID, "test", "a" * 64, 1, store.now_iso()),
        )
        reg = {"registration_id": "node-a", "machine_id": "node-a", "hostname": "node-a",
               "role_hint": None, "installer_version": None,
               "hardware_json": NODE_A_HW, "os": None, "lxd": None}
        self.node_id, _ = store.upsert_node(
            self.conn, PROJECT_ID, reg, "compute", store.sha256("node-a"),
            None, store.now_iso())
        self.conn.commit()

    def _commit(self, plan_id, cpu, ram, disk, vm_ids=None):
        store.commit_allocation(self.conn, PROJECT_ID, plan_id, self.node_id,
                                cpu, ram, disk, store.now_iso())
        if vm_ids:
            store.set_allocation_instances(self.conn, PROJECT_ID, plan_id,
                                           self.node_id, vm_ids)
        self.conn.commit()

    def _free_ram(self):
        row = self.conn.execute(
            "SELECT hardware_json FROM nodes WHERE id = ?", (self.node_id,)).fetchone()
        import json
        cap = health.capacity_of(json.loads(row["hardware_json"]),
                                 *self._allocated())
        return cap["ram_free_gb"]

    def _allocated(self):
        a = store.allocated_capacity(self.conn, self.node_id)
        return (a["cpu_allocated"], a["ram_allocated_gb"], a["disk_allocated_gb"])

    # ------------------------------------------------------------------ cases
    def test_never_applied_orphan_is_released_and_frees_capacity(self):
        self._commit("ORPHAN0000000000000000000A", 2, 2, 32)  # vm_ids NULL
        self.assertEqual(self._free_ram(), 3)

        out = server_mod.release_orphaned_reservations(
            self.conn, PROJECT_ID, live_plan_ids=())
        self.conn.commit()

        self.assertEqual(out["released_count"], 1)
        self.assertEqual(out["released"][0]["reason"], "plan_orphaned_restart")
        self.assertEqual(out["released"][0]["released"]["ram_allocated_gb"], 2)
        self.assertEqual(self._allocated(), (0, 0, 0))
        self.assertEqual(self._free_ram(), 5)
        # soft-released, not deleted: the audit trail survives.
        row = self.conn.execute(
            "SELECT released_at, release_reason FROM vm_allocations"
            " WHERE plan_id = 'ORPHAN0000000000000000000A'").fetchone()
        self.assertIsNotNone(row)
        self.assertTrue(row["released_at"])
        self.assertEqual(row["release_reason"], "plan_orphaned_restart")

    def test_a_plan_this_process_still_holds_is_left_alone(self):
        self._commit("LIVE000000000000000000000A", 2, 2, 8)
        out = server_mod.release_orphaned_reservations(
            self.conn, PROJECT_ID,
            live_plan_ids={"LIVE000000000000000000000A"})
        self.conn.commit()
        self.assertEqual(out["released_count"], 0)
        self.assertEqual(self._allocated(), (2, 2, 8))

    def test_a_materialised_commit_is_left_to_reconcile(self):
        # instances recorded -> reconcile owns it (absence is provable there).
        self._commit("RUNNING0000000000000000000A", 2, 2, 8,
                     vm_ids=["termini-running-0-0", "termini-running-0-1"])
        out = server_mod.release_orphaned_reservations(
            self.conn, PROJECT_ID, live_plan_ids=())
        self.conn.commit()
        self.assertEqual(out["released_count"], 0)
        self.assertEqual(self._allocated(), (2, 2, 8))

    def test_release_is_idempotent(self):
        self._commit("ORPHAN0000000000000000000B", 2, 2, 32)
        first = server_mod.release_orphaned_reservations(
            self.conn, PROJECT_ID, live_plan_ids=())
        second = server_mod.release_orphaned_reservations(
            self.conn, PROJECT_ID, live_plan_ids=())
        self.conn.commit()
        self.assertEqual(first["released_count"], 1)
        self.assertEqual(second["released_count"], 0)

    # ---------------------------------------------------------------- effect
    def test_the_orphan_is_what_blocked_the_plain_nl_deploy(self):
        """Before the sweep the NL deploy is refused; after it, accepted."""
        running = "RUNNING0000000000000000000A"
        orphan = "ORPHAN0000000000000000000C"
        self._commit(running, 2, 2, 8, vm_ids=["termini-running-0-0", "termini-running-0-1"])
        self._commit(orphan, 2, 2, 32)  # never applied -> free RAM 1 GiB

        req = [{"type": "agent_vm", "count": 2, **DEFAULT_AGENT_VM,
                "passthrough_gpu": False, "target_node": "auto"}]
        before = server_mod.ControllerHandler._build_plan(
            _Handler(), self.conn, PROJECT_ID,
            clamp_validate(req, DEFAULT_HOST_CAPS), None)
        self.assertEqual(before["outcome"], "rejected")
        self.assertIn("exceeds_node_ram_ceiling",
                      [r["reason"] for r in before["reject_reasons"]])

        server_mod.release_orphaned_reservations(
            self.conn, PROJECT_ID, live_plan_ids={running})
        self.conn.commit()

        after = server_mod.ControllerHandler._build_plan(
            _Handler(), self.conn, PROJECT_ID,
            clamp_validate(req, DEFAULT_HOST_CAPS), None)
        self.assertEqual(after["outcome"], "accepted", after.get("reject_reasons"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
