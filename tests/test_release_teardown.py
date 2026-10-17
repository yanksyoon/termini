#!/usr/bin/env python3
"""Release must tear down the instances it materialised (t_28ab1be5).

Found live on node-a, 2026-10-17: `POST /v1/projects/{pid}/vms/release` put the
plan's capacity back in the pool and left the LXD containers RUNNING. The node's
accounting said "free", the node's RAM said otherwise, and nothing in `GET /nodes`
could show the difference — a slow leak on an 8 GiB box that made "reset for the
next visitor" a lie.

This file pins the repair:

  * release deletes exactly what `vm_allocations.vm_ids_json` recorded for the
    plan, on that plan's node, with `force=True` (a released plan's instances are
    typically still RUNNING and LXD refuses to delete those);
  * capacity release stays the primary effect — a per-instance delete failure is
    reported in `failed[]` and never undoes or aborts it;
  * a second release is `already_released` and deletes nothing a second time;
  * `vm_ids_json IS NULL` (apply interrupted before it recorded anything) is
    reported as `unknown_instances: true` — visible, never guessed at;
  * containers that are not recorded against the plan (`demo-workload`, another
    worker's instance) are untouched;
  * the startup/reconcile path keeps working and never deletes anything of its
    own (it releases capacity for instances it finds GONE, so there is nothing
    left to tear down there);
  * the operator log row names what was torn down.
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


def _agent(count=1, *, vcpus=1, ram_gb=1, disk_gb=8):
    return {"type": "agent_vm", "count": count, "vcpus": vcpus, "ram_gb": ram_gb,
            "disk_gb": disk_gb, "target_node": "auto"}


class ReleaseTeardownTest(unittest.TestCase):
    """Drives the real controller over HTTP against a real (stub) LXD server."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-teardown-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = os.path.join(self.tmp, "controller.db")
        db.migrate(self.db_path)
        self.cfg = _load_cfg(self.db_path)
        with db.session(self.db_path) as conn:
            store.seed_bootstrap_tokens(conn, list(self.cfg.bootstrap_tokens), store.now_iso())
            self.project, _ = store.ensure_project(conn, self.cfg, store.now_iso())

        self.lxd = StubLxd()
        self.lxd.start()
        self.addCleanup(self.lxd.stop)

        self.broker = events.EventBroker()
        self.logs = logs.OperatorLogs(self.broker, ring_size=200)
        self.srv = server_mod.ControllerServer(
            ("127.0.0.1", 0), self.cfg, self.project, self.broker, self.logs,
            inventory.Inventory.load(None, "ubuntu", 22), terminal.TerminalManager(),
            sshca.SSHCA(None),
        )
        self.port = self.srv.server_address[1]
        self._thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self._thread.start()
        self.addCleanup(self._stop)
        self.node_id = self._add_node()

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
            with urllib.request.urlopen(req, timeout=30) as r:
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

    def _add_node(self, reg_id="compute-1"):
        with db.session(self.db_path) as conn:
            reg = {
                "registration_id": reg_id, "machine_id": reg_id, "hostname": reg_id,
                "role_hint": None, "installer_version": None, "hardware_json": COMPUTE_HW,
                "os": None, "lxd": None, "lxd_endpoint": self.lxd.base_url,
            }
            node_id, _ = store.upsert_node(
                conn, PROJECT_ID, reg, "compute", store.sha256(reg_id), None, store.now_iso())
        return node_id

    def _conn(self):
        return db.connect(self.db_path)

    def _plan(self, allocations):
        return self._post(f"/v1/projects/{PROJECT_ID}/intents", {"spec": {"allocations": allocations}})

    def _apply(self, plan_id):
        return self._post(f"/v1/projects/{PROJECT_ID}/vms/apply", {"plan_id": plan_id})

    def _release(self, plan_id, reason="test"):
        return self._post(f"/v1/projects/{PROJECT_ID}/vms/release",
                          {"plan_id": plan_id, "reason": reason})

    def _claims(self):
        conn = self._conn()
        try:
            return store.allocated_capacity(conn, self.node_id)["ram_allocated_gb"]
        finally:
            conn.close()

    def _materialise(self, count=1, ram_gb=1):
        st, plan = self._plan([_agent(count, ram_gb=ram_gb)])
        self.assertEqual(st, 200, plan)
        st, applied = self._apply(plan["plan_id"])
        self.assertEqual(st, 202, applied)
        self.assertEqual(applied["state"], "ready", applied)
        for name in applied["vm_ids"]:
            self.assertIn(name, self.lxd.instances)
        return plan["plan_id"], applied["vm_ids"]

    def _log_rows(self):
        conn = self._conn()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT severity, message, remediation_json FROM operator_logs"
                " ORDER BY log_id").fetchall()]
        finally:
            conn.close()

    # ------------------------------------------------------- acceptance 1 + 2
    def test_release_deletes_the_instance_and_returns_the_capacity(self):
        plan_id, vm_ids = self._materialise(1, ram_gb=1)
        self.assertEqual(len(vm_ids), 1)
        name = vm_ids[0]
        self.assertEqual(self.lxd.state_of(name), "Running")     # release meets a RUNNING VM
        self.assertEqual(self._claims(), 1)                      # capacity is committed
        # The apply itself never deletes anything: only a materialisation FAILURE
        # rolls back. So at this point the teardown must not have happened.
        self.assertEqual(self.lxd.delete_requests(), [])

        st, body = self._release(plan_id, "teardown_proof")
        self.assertEqual(st, 200, body)
        self.assertEqual(body["status"], "released")             # capacity: the primary effect
        self.assertEqual(body["released"]["ram_allocated_gb"], 1)
        self.assertEqual(body["rows_released"], 1)
        self.assertEqual(body["deleted"], [name])                # the instance is gone
        self.assertEqual(body["failed"], [])
        self.assertFalse(body["unknown_instances"])

        self.assertNotIn(name, self.lxd.instances)               # LXD really lost it
        self.assertNotIn(name, self.lxd.instance_states)
        delete_paths = [p for (_m, p, _b) in self.lxd.delete_requests()]
        self.assertIn(f"/1.0/instances/{name}", delete_paths)
        # force=True stops it first — LXD refuses to delete a RUNNING instance.
        self.assertIn(("PUT", f"/1.0/instances/{name}/state",
                       {"action": "stop", "force": True}), self.lxd.requests)
        self.assertEqual(self._claims(), 0)                      # capacity back in the pool

    def test_second_release_is_already_released_and_deletes_nothing_twice(self):
        plan_id, vm_ids = self._materialise(1)
        name = vm_ids[0]
        st, first = self._release(plan_id)
        self.assertEqual((st, first["status"]), (200, "released"))
        self.assertEqual(first["deleted"], [name])
        deletes_after_first = len(self.lxd.delete_requests())
        self.assertEqual(deletes_after_first, 1)

        st, second = self._release(plan_id)
        self.assertEqual(st, 200, second)                        # never an error
        self.assertEqual(second["status"], "already_released")
        self.assertEqual(second["rows_released"], 0)
        self.assertEqual(second["deleted"], [])
        self.assertEqual(second["failed"], [])
        self.assertFalse(second["unknown_instances"])
        self.assertEqual(len(self.lxd.delete_requests()), deletes_after_first,
                         "a re-release must not try to delete anything a second time")
        self.assertEqual(self._claims(), 0)

    # ----------------------------------------------------------- acceptance 3
    def test_a_plan_with_no_recorded_instances_releases_and_reports_unknown(self):
        # Committed, never applied (or apply interrupted before it recorded the
        # names): `vm_ids_json` stays NULL. Release must succeed — the reservation
        # is real — and must SAY it cannot know what to tear down.
        st, plan = self._plan([_agent(1, ram_gb=2)])
        self.assertEqual(st, 200, plan)
        plan_id = plan["plan_id"]
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT vm_ids_json FROM vm_allocations WHERE plan_id = ?", (plan_id,)).fetchone()
            self.assertIsNone(row["vm_ids_json"])
        finally:
            conn.close()

        st, body = self._release(plan_id)
        self.assertEqual(st, 200, body)
        self.assertEqual(body["status"], "released")
        self.assertEqual(body["released"]["ram_allocated_gb"], 2)
        self.assertTrue(body["unknown_instances"])
        self.assertEqual(body["deleted"], [])
        self.assertEqual(body["failed"], [])
        self.assertEqual(self.lxd.delete_requests(), [], "names must never be guessed")
        self.assertEqual(self._claims(), 0)

        # ...and the operator log says so, at warn level, instead of staying quiet.
        rows = self._log_rows()
        release_rows = [r for r in rows if "released plan" in r["message"]]
        self.assertTrue(release_rows, rows)
        self.assertEqual(release_rows[-1]["severity"], "warn")
        self.assertIn("vm_ids unknown", release_rows[-1]["message"])
        self.assertTrue(json.loads(release_rows[-1]["remediation_json"])["teardown"]
                        ["unknown_instances"])

    # ----------------------------------------------------------- acceptance 4
    def test_unrelated_containers_are_untouched(self):
        # The node is a shared resource: `demo-workload` is the always-on agent and
        # `verif-proof` is another worker's instance. Neither is in the plan's
        # recorded vm_ids, so neither may be deleted.
        self.lxd.instances["demo-workload"] = {}
        self.lxd.instances["verif-proof"] = {}
        self.lxd.set_state("demo-workload", "Running")
        plan_id, vm_ids = self._materialise(1)
        name = vm_ids[0]

        st, body = self._release(plan_id)
        self.assertEqual(st, 200, body)
        self.assertEqual(body["deleted"], [name])
        self.assertIn("demo-workload", self.lxd.instances)
        self.assertIn("verif-proof", self.lxd.instances)
        self.assertEqual(self.lxd.state_of("demo-workload"), "Running")
        touched = {p for (_m, p, _b) in self.lxd.delete_requests()}
        self.assertEqual(touched, {f"/1.0/instances/{name}"})

    # ------------------------------------------------- best-effort, not silent
    def test_a_failed_delete_is_reported_and_does_not_abort_the_release(self):
        plan_id, vm_ids = self._materialise(2)
        self.assertEqual(len(vm_ids), 2)
        stuck, removable = vm_ids[1], vm_ids[0]
        self.lxd.fail_delete.add(stuck)

        st, body = self._release(plan_id)
        self.assertEqual(st, 200, body)                          # not a 5xx
        self.assertEqual(body["status"], "released")             # capacity still returned
        self.assertEqual(body["deleted"], [removable])
        self.assertEqual(len(body["failed"]), 1, body)
        self.assertEqual(body["failed"][0]["name"], stuck)
        self.assertEqual(body["failed"][0]["node_id"], self.node_id)
        self.assertIn("device busy", body["failed"][0]["error"])
        self.assertEqual(self._claims(), 0)
        # The one that refused to die is STILL THERE — and therefore visible, not
        # counted as deleted.
        self.assertIn(stuck, self.lxd.instances)
        self.assertNotIn(removable, self.lxd.instances)

        rows = [r for r in self._log_rows() if "released plan" in r["message"]]
        self.assertTrue(rows)
        self.assertEqual(rows[-1]["severity"], "warn")
        self.assertIn(stuck, rows[-1]["message"])
        self.assertIn("could NOT be deleted", rows[-1]["message"])

    def test_a_deleted_instance_counts_as_torn_down(self):
        # LXD 404 -> executor `already_applied`: the postcondition ("it is gone")
        # holds, so the name is reported as deleted rather than as a failure.
        plan_id, vm_ids = self._materialise(1)
        name = vm_ids[0]
        del self.lxd.instances[name]                             # someone else got there first
        self.lxd.set_state(name, "Stopped")

        st, body = self._release(plan_id)
        self.assertEqual(st, 200, body)
        self.assertEqual(body["deleted"], [name])
        self.assertEqual(body["failed"], [])

    # ------------------------------------------------ reconcile must not trip
    def test_reconcile_still_releases_a_commit_whose_instances_are_gone(self):
        plan_id, vm_ids = self._materialise(1, ram_gb=4)
        name = vm_ids[0]
        del self.lxd.instances[name]                             # operator/node removed it

        st, rec = self._post(f"/v1/projects/{PROJECT_ID}/vms/reconcile", {})
        self.assertEqual(st, 200, rec)
        self.assertEqual(rec["released_count"], 1, rec)
        self.assertEqual(rec["released"][0]["reason"], "instances_gone")
        self.assertEqual(self._claims(), 0)
        # Reconcile releases capacity for instances it has PROVEN absent, so it
        # has nothing to tear down and must not start deleting: the teardown is a
        # release-path effect only.
        self.assertEqual(self.lxd.delete_requests(), [])

    def test_reconcile_leaves_a_live_materialised_plan_alone(self):
        plan_id, vm_ids = self._materialise(1)
        st, rec = self._post(f"/v1/projects/{PROJECT_ID}/vms/reconcile", {})
        self.assertEqual(st, 200, rec)
        self.assertEqual(rec["released_count"], 0, rec)
        self.assertEqual(self._claims(), 1)
        self.assertIn(vm_ids[0], self.lxd.instances)
        self.assertEqual(self.lxd.delete_requests(), [])

    # ----------------------------------------------------------- store helper
    def test_live_allocation_rows_distinguishes_null_from_recorded(self):
        st, plan = self._plan([_agent(1)])
        self.assertEqual(st, 200, plan)
        plan_id = plan["plan_id"]
        conn = self._conn()
        try:
            rows = store.live_allocation_rows(conn, PROJECT_ID, plan_id=plan_id)
            self.assertEqual(len(rows), 1)
            self.assertFalse(rows[0]["vm_ids_recorded"])         # NULL: unknown
            self.assertEqual(rows[0]["vm_ids"], [])
            self.assertEqual(rows[0]["node_id"], self.node_id)

            store.set_allocation_instances(conn, PROJECT_ID, plan_id, self.node_id, [])
            conn.commit()
            rows = store.live_allocation_rows(conn, PROJECT_ID, plan_id=plan_id)
            self.assertTrue(rows[0]["vm_ids_recorded"])          # recorded: nothing to do
            self.assertEqual(rows[0]["vm_ids"], [])

            store.set_allocation_instances(conn, PROJECT_ID, plan_id, self.node_id,
                                           ["termini-x-0-0"])
            conn.commit()
            rows = store.live_allocation_rows(conn, PROJECT_ID, plan_id=plan_id)
            self.assertEqual(rows[0]["vm_ids"], ["termini-x-0-0"])

            store.release_allocations(conn, PROJECT_ID, plan_id=plan_id, reason="t")
            self.assertEqual(store.live_allocation_rows(conn, PROJECT_ID, plan_id=plan_id), [])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
