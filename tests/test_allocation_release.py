#!/usr/bin/env python3
"""Allocation release + node decommission (t_33c7e847).

Committed `agent_vm` capacity used to be permanent: the only way capacity entered
`vm_allocations` was the §5.2 step 7 commit at plan time, and nothing ever
decremented or deleted it. A demo that scaled a few times filled a node's
allocatable RAM and every later plan was rejected 422
`exceeds_node_ram_ceiling` forever — unrecoverable without wiping the DB (which
destroys the audit trail). The same gap let phantom OFFLINE nodes accumulate so
the dashboard's fleet contradicted the deck.

This file pins the repair:

  * an explicit release path — `POST /v1/projects/{pid}/vms/release` — that puts a
    plan's reservation back in the pool, idempotently;
  * the release genuinely returns capacity: a plan that 422'd BEFORE the release
    is accepted AFTER it (THE before/after test);
  * a repeat release is a clean `already_released`, an unknown plan a clean `404`,
    a malformed plan_id a clean `422` — never a 5xx crash;
  * a node decommission route retires a node out of the fleet listing;
  * automatic self-heal — a commit whose materialised instances are gone is
    released by reconciliation, while a commit that was never materialised, or a
    node that cannot be reached, is left alone (absence must be proven).
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
CONTROLLER = REPO / "controller"
sys.path.insert(0, str(CONTROLLER))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from app import config, db, events, inventory, logs, server as server_mod, sshca, store, terminal, ulid  # noqa: E402
from stub_lxd import StubLxd  # noqa: E402

GiB = 1024 ** 3
PROJECT_ID = "01J0DEM0WARM000000000000AB"
PROJECT_TOKEN = "test-project-token"

# 8 logical cores / 32 GiB / 256 GiB => allocatable 30 GiB RAM (32 − 2 reserve),
# 16 vCPU (2× (8 − int(8×0.1))), 231 GiB disk. Mirrors test_capacity_clamp.
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
        "TERMINI_LXD_CREATE_TIMEOUT_S": "10",
        "TERMINI_HOST": "127.0.0.1",
        "TERMINI_PORT": "0",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        return config.Config.load()


def _compute_agent(count, *, vcpus=2, ram_gb=4, disk_gb=8):
    return {"type": "agent_vm", "count": count, "vcpus": vcpus, "ram_gb": ram_gb,
            "disk_gb": disk_gb, "target_node": "auto"}


# --------------------------------------------------------------------------- store

class _StoreBase(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))
        db.migrate(self.path)
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "INSERT INTO projects (id, name, project_token_hash, max_inference_nodes, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (PROJECT_ID, "test", "a" * 64, 1, store.now_iso()),
        )
        reg = {
            "registration_id": "compute-1", "machine_id": "compute-1",
            "hostname": "compute-1", "role_hint": None, "installer_version": None,
            "hardware_json": COMPUTE_HW, "os": None, "lxd": None, "lxd_endpoint": None,
        }
        self.node_id, _ = store.upsert_node(
            self.conn, PROJECT_ID, reg, "compute", store.sha256("tok"), None, store.now_iso())
        self.conn.commit()

    def _commit(self, plan_id, cpu=6, ram=12, disk=24):
        store.commit_allocation(self.conn, PROJECT_ID, plan_id, self.node_id,
                                cpu, ram, disk, store.now_iso())
        self.conn.commit()

    def _alloc(self):
        return store.allocated_capacity(self.conn, self.node_id)


class ReleaseAllocationStoreTest(_StoreBase):
    def test_release_returns_capacity_and_is_idempotent(self):
        self._commit("PLAN000000000000000000000A")
        self.assertEqual(self._alloc(),
                         {"cpu_allocated": 6, "ram_allocated_gb": 12, "disk_allocated_gb": 24})

        first = store.release_allocations(self.conn, PROJECT_ID,
                                          plan_id="PLAN000000000000000000000A", reason="test")
        self.assertTrue(first["had_commit"])
        self.assertFalse(first["already_released"])
        self.assertEqual(first["rows_released"], 1)
        self.assertEqual(first["released"],
                         {"cpu_allocated": 6, "ram_allocated_gb": 12, "disk_allocated_gb": 24})
        self.assertEqual(self._alloc(),
                         {"cpu_allocated": 0, "ram_allocated_gb": 0, "disk_allocated_gb": 0})

        # Idempotent: a repeat release reports already_released, never a double
        # decrement (capacity can never go negative) and never an error.
        again = store.release_allocations(self.conn, PROJECT_ID,
                                          plan_id="PLAN000000000000000000000A")
        self.assertTrue(again["had_commit"])
        self.assertTrue(again["already_released"])
        self.assertEqual(again["rows_released"], 0)
        self.assertEqual(again["released"],
                         {"cpu_allocated": 0, "ram_allocated_gb": 0, "disk_allocated_gb": 0})
        self.assertEqual(self._alloc(),
                         {"cpu_allocated": 0, "ram_allocated_gb": 0, "disk_allocated_gb": 0})

    def test_release_keeps_the_commit_row_so_the_audit_trail_survives(self):
        self._commit("PLAN000000000000000000000B")
        store.release_allocations(self.conn, PROJECT_ID,
                                  plan_id="PLAN000000000000000000000B", reason="scaled_down")
        row = self.conn.execute(
            "SELECT ram_allocated_gb, released_at, release_reason FROM vm_allocations").fetchone()
        self.assertIsNotNone(row)                       # soft release, NOT deleted
        self.assertEqual(row["ram_allocated_gb"], 12)   # the original commit is preserved
        self.assertTrue(row["released_at"])
        self.assertEqual(row["release_reason"], "scaled_down")

    def test_release_of_unknown_plan_reports_no_commit(self):
        r = store.release_allocations(self.conn, PROJECT_ID,
                                      plan_id="PLAN000000000000000000000Z")
        self.assertFalse(r["had_commit"])
        self.assertFalse(r["already_released"])
        self.assertEqual(r["rows_released"], 0)

    def test_release_requires_a_filter(self):
        with self.assertRaises(ValueError):
            store.release_allocations(self.conn, PROJECT_ID)

    def test_failed_materialisation_soft_releases_and_keeps_the_row(self):
        self._commit("PLAN000000000000000000000C", cpu=2, ram=4, disk=8)
        released = store.release_allocation(self.conn, PROJECT_ID,
                                            "PLAN000000000000000000000C", self.node_id, 2, 4, 8)
        self.assertTrue(released)
        self.assertEqual(self._alloc(),
                         {"cpu_allocated": 0, "ram_allocated_gb": 0, "disk_allocated_gb": 0})
        row = self.conn.execute(
            "SELECT released_at, release_reason FROM vm_allocations").fetchone()
        self.assertIsNotNone(row)                       # not deleted
        self.assertTrue(row["released_at"])
        self.assertEqual(row["release_reason"], "materialisation_failed")


# --------------------------------------------------------------------------- http

class _HttpBase(unittest.TestCase):
    """Drives the real ControllerServer over HTTP (routing + auth + envelopes)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-release-")
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
    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _post(self, path, body=None, token=PROJECT_TOKEN):
        data = json.dumps(body).encode() if body is not None else b""
        req = urllib.request.Request(
            self._url(path), data=data,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
            method="POST")
        return self._call(req)

    def _get(self, path, token=PROJECT_TOKEN):
        req = urllib.request.Request(
            self._url(path), headers={"Authorization": "Bearer " + token}, method="GET")
        return self._call(req)

    @staticmethod
    def _call(req):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
                try:
                    return e.code, json.loads(raw)
                except json.JSONDecodeError:
                    return e.code, {}
            finally:
                e.close()

    def _add_node(self, reg_id="compute-1", hw=None, role="compute", lxd_endpoint=None):
        with db.session(self.db_path) as conn:
            reg = {
                "registration_id": reg_id, "machine_id": reg_id, "hostname": reg_id,
                "role_hint": None, "installer_version": None,
                "hardware_json": hw or COMPUTE_HW, "os": None, "lxd": None,
                "lxd_endpoint": lxd_endpoint,
            }
            node_id, _ = store.upsert_node(
                conn, PROJECT_ID, reg, role, store.sha256(reg_id), None, store.now_iso())
        return node_id

    def _conn(self):
        return db.connect(self.db_path)

    def _plan(self, allocations):
        """Submit a structured intent and return (status, body)."""
        return self._post(f"/v1/projects/{PROJECT_ID}/intents", {"spec": {"allocations": allocations}})


class ReleaseRouteTest(_HttpBase):
    def setUp(self):
        super().setUp()
        self.node_id = self._add_node()

    def test_release_returns_capacity_and_is_idempotent(self):
        st, plan = self._plan([_compute_agent(3, ram_gb=10)])
        self.assertEqual(st, 200, plan)
        plan_id = plan["plan_id"]

        st, body = self._post(f"/v1/projects/{PROJECT_ID}/vms/release", {"plan_id": plan_id})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["status"], "released")
        self.assertEqual(body["rows_released"], 1)
        self.assertEqual(body["released"]["ram_allocated_gb"], 30)   # 3 × 10
        self.assertEqual(body["released"]["cpu_allocated"], 6)       # 3 × 2

        # idempotent: no crash, no double decrement
        st, again = self._post(f"/v1/projects/{PROJECT_ID}/vms/release", {"plan_id": plan_id})
        self.assertEqual(st, 200, again)
        self.assertEqual(again["status"], "already_released")
        self.assertEqual(again["rows_released"], 0)
        self.assertEqual(again["released"],
                         {"cpu_allocated": 0, "ram_allocated_gb": 0, "disk_allocated_gb": 0})

    def test_release_returns_capacity_so_a_plan_that_previously_422d_now_succeeds(self):
        # BEFORE: one plan claims the node's whole allocatable RAM (30 GiB).
        st, p1 = self._plan([_compute_agent(3, ram_gb=10)])
        self.assertEqual(st, 200, p1)
        self.assertEqual(p1["outcome"], "accepted", p1)

        # ... so the headline-shaped plan no longer fits: 3 × 4 GiB against a
        # 0 GiB free pool clamps below the 1 GiB floor -> 422.
        st, rejected = self._plan([_compute_agent(3, ram_gb=4)])
        self.assertEqual(st, 422, rejected)
        self.assertEqual(rejected["error"]["code"], "GUARDRAIL_REJECTED")
        reasons = [r["reason"] for r in rejected["error"]["details"]]
        self.assertIn("exceeds_node_ram_ceiling", reasons)

        # RELEASE the stale plan's commitment.
        st, rel = self._post(f"/v1/projects/{PROJECT_ID}/vms/release",
                             {"plan_id": p1["plan_id"], "reason": "stale_demo_state"})
        self.assertEqual(st, 200, rel)
        self.assertEqual(rel["status"], "released")

        # AFTER: the exact plan that 422'd is now accepted.
        st, accepted = self._plan([_compute_agent(3, ram_gb=4)])
        self.assertEqual(st, 200, accepted)
        self.assertEqual(accepted["outcome"], "accepted", accepted)

    def test_repeat_release_and_unknown_are_clean_errors_not_crashes(self):
        # unknown plan_id (never committed) -> clean 404, not 500
        st, body = self._post(f"/v1/projects/{PROJECT_ID}/vms/release",
                              {"plan_id": ulid.new_ulid()})
        self.assertEqual(st, 404, body)
        self.assertEqual(body["error"]["code"], "NOT_FOUND")

        # malformed plan_id -> clean 422
        st, body = self._post(f"/v1/projects/{PROJECT_ID}/vms/release", {"plan_id": "not-a-ulid"})
        self.assertEqual(st, 422, body)
        self.assertEqual(body["error"]["code"], "INVALID_ARGUMENT")

        # missing plan_id -> clean 422
        st, body = self._post(f"/v1/projects/{PROJECT_ID}/vms/release", {})
        self.assertEqual(st, 422, body)
        self.assertEqual(body["error"]["code"], "INVALID_ARGUMENT")

        # a real commit released twice is a clean pair of 200s (above), and a
        # *deleted* commit released again is still a clean 200 `already_released`.
        _, plan = self._plan([_compute_agent(1)])
        st, first = self._post(f"/v1/projects/{PROJECT_ID}/vms/release",
                               {"plan_id": plan["plan_id"]})
        self.assertEqual((st, first["status"]), (200, "released"))
        st, second = self._post(f"/v1/projects/{PROJECT_ID}/vms/release",
                                {"plan_id": plan["plan_id"]})
        self.assertEqual((st, second["status"]), (200, "already_released"))

    def test_release_requires_a_project_token(self):
        st, body = self._post(f"/v1/projects/{PROJECT_ID}/vms/release",
                              {"plan_id": ulid.new_ulid()}, token="wrong-token")
        self.assertEqual(st, 403, body)


class DecommissionRouteTest(_HttpBase):
    def test_decommission_removes_the_node_from_the_fleet_listing(self):
        node_id = self._add_node("rt-verify-01")
        st, listing = self._get(f"/v1/projects/{PROJECT_ID}/nodes")
        self.assertEqual(st, 200)
        self.assertIn(node_id, [n["node_id"] for n in listing["nodes"]])

        st, body = self._post(
            f"/v1/projects/{PROJECT_ID}/nodes/{node_id}/decommission",
            {"reason": "throwaway test node"})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["status"], "decommissioned")

        st, listing = self._get(f"/v1/projects/{PROJECT_ID}/nodes")
        self.assertEqual(st, 200)
        self.assertNotIn(node_id, [n["node_id"] for n in listing["nodes"]])

        # idempotent, and an unknown node is a clean 404
        st, again = self._post(f"/v1/projects/{PROJECT_ID}/nodes/{node_id}/decommission", {})
        self.assertEqual((st, again["status"]), (200, "already_decommissioned"))
        st, body = self._post(
            f"/v1/projects/{PROJECT_ID}/nodes/{ulid.new_ulid()}/decommission", {})
        self.assertEqual(st, 404, body)

    def test_decommission_releases_the_nodes_committed_capacity(self):
        node_id = self._add_node("compute-1")
        _, plan = self._plan([_compute_agent(3, ram_gb=10)])

        conn = self._conn()
        try:
            self.assertEqual(
                store.allocated_capacity(conn, node_id)["ram_allocated_gb"], 30)
        finally:
            conn.close()

        _, before = self._get(f"/v1/projects/{PROJECT_ID}/nodes/{node_id}")
        self.assertEqual(before["capacity"]["ram_allocated_gb"], 30)

        st, body = self._post(f"/v1/projects/{PROJECT_ID}/nodes/{node_id}/decommission", {})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["released"]["ram_allocated_gb"], 30)

        conn = self._conn()
        try:
            self.assertEqual(
                store.allocated_capacity(conn, node_id)["ram_allocated_gb"], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS n FROM vm_allocations").fetchone()["n"], 1)
        finally:
            conn.close()


class ReconcileTest(_HttpBase):
    """Automatic self-heal: a commit whose instances are gone stops claiming capacity."""

    def setUp(self):
        super().setUp()
        self.lxd = StubLxd()
        self.lxd.start()
        self.addCleanup(self.lxd.stop)
        self.node_id = self._add_node("compute-1", lxd_endpoint=self.lxd.base_url)

    def _apply(self, plan_id):
        return self._post(f"/v1/projects/{PROJECT_ID}/vms/apply", {"plan_id": plan_id})

    def _claims(self):
        conn = self._conn()
        try:
            return store.allocated_capacity(conn, self.node_id)["ram_allocated_gb"]
        finally:
            conn.close()

    def test_reconcile_releases_capacity_when_the_materialised_instances_are_gone(self):
        _, plan = self._plan([_compute_agent(1, ram_gb=4)])
        st, applied = self._apply(plan["plan_id"])
        self.assertEqual(st, 202, applied)
        self.assertEqual(applied["state"], "ready", applied)
        name = applied["vm_ids"][0]
        self.assertIn(name, self.lxd.instances)
        self.assertEqual(self._claims(), 4)          # claimed while the VM exists

        # The instance goes away (operator delete / node reboot) — the classic
        # stale-commit that used to saturate the demo permanently.
        del self.lxd.instances[name]

        st, rec = self._post(f"/v1/projects/{PROJECT_ID}/vms/reconcile", {})
        self.assertEqual(st, 200, rec)
        self.assertEqual(rec["released_count"], 1, rec)
        self.assertEqual(rec["released"][0]["reason"], "instances_gone")
        self.assertEqual(self._claims(), 0)          # capacity genuinely returned

    def test_reconcile_keeps_capacity_while_the_instances_exist(self):
        _, plan = self._plan([_compute_agent(1, ram_gb=4)])
        st, applied = self._apply(plan["plan_id"])
        self.assertEqual(applied["state"], "ready", applied)

        st, rec = self._post(f"/v1/projects/{PROJECT_ID}/vms/reconcile", {})
        self.assertEqual(st, 200, rec)
        self.assertEqual(rec["released_count"], 0, rec)
        self.assertEqual(self._claims(), 4)

    def test_reconcile_leaves_a_commit_that_was_never_applied(self):
        # Committed but not materialised: nothing exists to be absent, so the
        # reservation stands (reconcile must not release it).
        _, plan = self._plan([_compute_agent(1, ram_gb=4)])
        st, rec = self._post(f"/v1/projects/{PROJECT_ID}/vms/reconcile", {})
        self.assertEqual(st, 200, rec)
        self.assertEqual(rec["released_count"], 0, rec)
        self.assertEqual(self._claims(), 4)

    def test_reconcile_skips_an_unreachable_node(self):
        # Absence must be PROVEN: a node we cannot read is not an empty node.
        _, plan = self._plan([_compute_agent(1, ram_gb=4)])
        conn = self._conn()
        try:
            store.set_allocation_instances(conn, PROJECT_ID, plan["plan_id"],
                                           self.node_id, ["termini-x-0-0"])
            conn.commit()
        finally:
            conn.close()

        class _Unreachable:
            def list_instance_names(self):
                return False, []

        conn = self._conn()
        try:
            result = server_mod.reconcile_allocations(
                conn, self.cfg, PROJECT_ID, executor_factory=lambda c, n: _Unreachable())
            self.assertEqual(result["released_count"], 0, result)
            self.assertEqual(
                store.allocated_capacity(conn, self.node_id)["ram_allocated_gb"], 4)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
