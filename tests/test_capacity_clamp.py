"""Per-node capacity clamp + allocation commit (F2, controller-api.md §5.2 step 6/7).

Proves the W7 gate finding is fixed:
  * `health.capacity_of` reports allocatable capacity (registered − reserve −
    allocated) instead of hardcoding `*_allocated = 0`;
  * `health.clamp_to_node_capacity` rejects the aggregate oversubscription
    (64 × 16 GiB RAM / 8 vCPU / 64 GiB disk) instead of accepting it unchanged;
  * `_build_plan` accounts for committed allocations across classes and commits
    accepted/clamped `agent_vm` capacity, so a later request sees less free
    capacity (all-or-nothing: a rejected class commits nothing).
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
from termini.intent import DEFAULT_HOST_CAPS, clamp_validate  # noqa: E402

GiB = 1024 ** 3

# Mirrors the harness's registered compute node (8 logical cores, 32 GiB, 256 GiB).
COMPUTE_HW = {
    "cpu": {"cores": 8, "threads": 8},
    "memory": {"total_bytes": 32 * GiB},
    "gpus": [],
    "disks": [{"size_bytes": 256 * GiB}],
}

PROJECT_ID = "01J0DEM0WARM000000000000AD"


class _Handler:
    """Stand-in `self` so `_build_plan` can be driven without a full server."""

    def _resolve_target(self, spec, nodes):
        return server_mod.ControllerHandler._resolve_target(None, spec, nodes)


class CapacityOfTest(unittest.TestCase):
    def test_allocatable_is_registered_minus_reserve(self):
        cap = health.capacity_of(COMPUTE_HW)
        self.assertEqual(cap["cpu_cores"], 8)
        self.assertEqual(cap["ram_gb"], 32)
        self.assertEqual(cap["disk_gb"], 256)
        # 2.0 × (8 − int(8×0.10)) − 0 = 16 vCPUs; 32 − 2 = 30 GiB; 256 − 25 = 231 GiB.
        self.assertEqual(cap["cpu_free"], 16)
        self.assertEqual(cap["ram_free_gb"], 30)
        self.assertEqual(cap["disk_free_gb"], 231)
        self.assertEqual(cap["cpu_allocated"], 0)
        self.assertEqual(cap["ram_allocated_gb"], 0)
        self.assertEqual(cap["disk_allocated_gb"], 0)

    def test_allocated_is_subtracted(self):
        cap = health.capacity_of(COMPUTE_HW, cpu_allocated=6, ram_allocated_gb=10, disk_allocated_gb=50)
        self.assertEqual(cap["cpu_free"], 10)      # 16 − 6
        self.assertEqual(cap["ram_free_gb"], 20)   # 32 − 2 − 10
        self.assertEqual(cap["disk_free_gb"], 181)  # 256 − 25 − 50


class ClampToNodeCapacityTest(unittest.TestCase):
    def setUp(self):
        self.cap = health.capacity_of(COMPUTE_HW)

    def test_aggregate_oversubscription_rejected(self):
        resolved, decisions, reject = health.clamp_to_node_capacity(64, 8, 16, 64, self.cap, index=0)
        self.assertEqual(reject, "exceeds_node_ram_ceiling")
        self.assertLess(resolved["ram_gb"], 1)
        self.assertTrue(any(d["reason"] == "exceeds_node_ram_ceiling" for d in decisions))

    def test_fitting_request_unchanged(self):
        resolved, decisions, reject = health.clamp_to_node_capacity(3, 2, 4, 8, self.cap)
        self.assertIsNone(reject)
        self.assertEqual(resolved, {"count": 3, "vcpus": 2, "ram_gb": 4, "disk_gb": 8})
        self.assertEqual(decisions, [])

    def test_per_vm_size_clamped_to_share(self):
        # 3 × 16 GiB = 48 GiB > 30 GiB free → each clamped to 30 // 3 = 10 GiB.
        resolved, decisions, reject = health.clamp_to_node_capacity(3, 2, 16, 8, self.cap)
        self.assertIsNone(reject)
        self.assertEqual(resolved["ram_gb"], 10)
        self.assertTrue(any(d["reason"] == "exceeds_node_ram_ceiling" for d in decisions))


class BuildPlanTest(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        db.migrate(self.path)
        self.conn = db.connect(self.path)
        self.conn.execute(
            "INSERT INTO projects (id, name, project_token_hash, max_inference_nodes, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (PROJECT_ID, "test", "a" * 64, 1, store.now_iso()),
        )
        reg = {
            "registration_id": "compute-1", "machine_id": "compute-1",
            "hostname": "compute-1", "role_hint": None, "installer_version": None,
            "hardware_json": COMPUTE_HW, "os": None, "lxd": None,
        }
        self.node_id, _ = store.upsert_node(
            self.conn, PROJECT_ID, reg, "compute", store.sha256("tok"), None, store.now_iso(),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _build(self, components):
        result = clamp_validate(components, DEFAULT_HOST_CAPS)
        return server_mod.ControllerHandler._build_plan(
            _Handler(), self.conn, PROJECT_ID, result, None,
        )

    def test_aggregate_oversubscription_rejected_and_not_committed(self):
        plan = self._build([{"type": "agent_vm", "count": 64, "vcpus": 8, "ram_gb": 16, "disk_gb": 64}])
        self.assertEqual(plan["outcome"], "rejected")
        alloc = plan["allocations"][0]
        self.assertEqual(alloc["status"], "rejected")
        self.assertEqual(alloc["guardrail"]["reject_reasons"][0]["reason"], "exceeds_node_ram_ceiling")
        # The reason is surfaced at plan level too, so the 422 `details` is observable.
        self.assertTrue(any(r["reason"] == "exceeds_node_ram_ceiling" for r in plan["reject_reasons"]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS n FROM vm_allocations").fetchone()["n"], 0)

    def test_within_capacity_accepted_and_committed(self):
        plan = self._build([{"type": "agent_vm", "count": 3, "vcpus": 2, "ram_gb": 4, "disk_gb": 8}])
        self.assertEqual(plan["outcome"], "accepted")
        self.conn.commit()
        alloc = store.allocated_capacity(self.conn, self.node_id)
        self.assertEqual(alloc["cpu_allocated"], 6)       # 3 × 2
        self.assertEqual(alloc["ram_allocated_gb"], 12)   # 3 × 4
        self.assertEqual(alloc["disk_allocated_gb"], 24)  # 3 × 8

    def test_commit_reduces_subsequent_free_capacity(self):
        p1 = self._build([{"type": "agent_vm", "count": 3, "vcpus": 2, "ram_gb": 4, "disk_gb": 8}])
        self.assertEqual(p1["outcome"], "accepted")
        self.conn.commit()
        # 30 − 12 = 18 GiB free now; 5 × 4 GiB = 20 > 18 → each clamped to 18 // 5 = 3.
        p2 = self._build([{"type": "agent_vm", "count": 5, "vcpus": 2, "ram_gb": 4, "disk_gb": 8}])
        self.assertEqual(p2["outcome"], "accepted")  # plan outcome is accepted|rejected only
        self.assertEqual(p2["allocations"][0]["status"], "clamped")
        self.assertEqual(p2["allocations"][0]["resolved"]["ram_gb"], 3)

    def test_rejected_class_commits_nothing(self):
        # First class fits; second class oversubscribes → all-or-nothing, no commit.
        plan = self._build([
            {"type": "agent_vm", "count": 1, "vcpus": 2, "ram_gb": 4, "disk_gb": 8},
            {"type": "agent_vm", "count": 64, "vcpus": 8, "ram_gb": 16, "disk_gb": 64},
        ])
        self.assertEqual(plan["outcome"], "rejected")
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS n FROM vm_allocations").fetchone()["n"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
