#!/usr/bin/env python3
"""Apply-plan materialisation (per-node LXD addressing + real vm_ids).

`_apply_plan` used to publish `state: "provisioning"` with `vm_ids: []` forever.
It now drives the LXD REST API of each allocation's resolved node and returns a
TERMINAL state with the real instance ids. This file pins the properties:

  * a node that reports its own endpoint is addressed at THAT endpoint (never the
    global URL); a node that reports none falls back to the global
    `TERMINI_LXD_BASE_URL` (the stub-demo path);
  * apply creates one LXD instance per committed VM and returns the real ids;
  * a failed/timed-out create is surfaced as failure, is never reported as
    success, and releases that allocation's committed capacity so the plan never
    claims an instance that does not exist;
  * a replayed apply is idempotent (a duplicate name is `already_applied`).

The pure resolver tests need no I/O; the apply tests drive `_apply_plan` directly
with a small harness (a scripted executor for the failure/replay paths) and, for
the addressing tests, a real `StubLxd` over HTTP.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER = REPO / "controller"
sys.path.insert(0, str(CONTROLLER))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from app import db, events, registration, server as server_mod, store  # noqa: E402
from app import logs as logs_mod  # noqa: E402
from app.operator import target as target_mod  # noqa: E402
from app.operator.models import ExecResult  # noqa: E402
from stub_lxd import StubLxd  # noqa: E402

GiB = 1024 ** 3
COMPUTE_HW = {
    "cpu": {"cores": 8, "threads": 8},
    "memory": {"total_bytes": 32 * GiB},
    "gpus": [],
    "disks": [{"size_bytes": 256 * GiB}],
}
PROJECT_ID = "01J0DEM0WARM000000000000AD"


# --------------------------------------------------------------------------- doubles

class _FakeExecutor:
    """Records create/delete calls; returns a scripted result per instance name."""

    def __init__(self, results=None, default="ok"):
        self.created: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self._results = results or {}
        self._default = default

    def create_instance(self, name, **kw):
        self.created.append((name, kw))
        r = self._results.get(name, self._default)
        if isinstance(r, tuple):
            return ExecResult(result=r[0], detail=r[1], method="POST", path="/1.0/instances")
        return ExecResult(result=r, method="POST", path="/1.0/instances")

    def delete_instance(self, name):
        self.deleted.append(name)
        return ExecResult(result="ok", method="DELETE", path=f"/1.0/instances/{name}")


class _Broker:
    def __init__(self):
        self.events: list[tuple[str, dict, str]] = []

    def publish(self, event, data, project_id):
        self.events.append((event, data, project_id))


class _ServerStub:
    def __init__(self, cfg, plans, broker, logs=None):
        self.cfg = cfg
        self.plans = plans
        self.broker = broker
        self.operator_logs = logs


class _Harness:
    """Minimal `self` so `ControllerHandler._apply_plan` can be driven directly."""

    def __init__(self, server, body):
        self.server = server
        self._body = body

    def _read_body(self):
        return dict(self._body)

    def _require_project(self, conn, project_id):
        return None


def _cfg(**over):
    base = dict(
        lxd_base_url="http://127.0.0.1:1",
        lxd_node_scheme="https",
        lxd_workload_image="agent-vm",
        lxd_create_timeout_s=5.0,
        lxd_client_cert=None,
        lxd_client_key=None,
        lxd_request_timeout_s=2.0,
        lxd_retry_max=1,
        lxd_retry_backoff_s=0.0,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def _alloc(index, node_id, *, count=1, vcpus=2, ram_gb=4, disk_gb=8,
           status="accepted", realized_as="vm"):
    return {
        "index": index,
        "status": status,
        "resolved": {"type": "agent_vm", "count": count, "vcpus": vcpus,
                     "ram_gb": ram_gb, "disk_gb": disk_gb},
        "guardrail": {"clamped_fields": [], "reject_reasons": []},
        "node_id": node_id,
        "realized_as": realized_as,
        "vm_ids": [],
    }


def _plan(plan_id, allocations, *, outcome="accepted"):
    return {
        "plan_id": plan_id, "request_id": plan_id, "project_id": PROJECT_ID,
        "outcome": outcome, "created_at": store.now_iso(), "allocations": allocations,
        "audit": {"actor": "human", "guardrail": "test"},
    }


# --------------------------------------------------------------------------- resolver

class EndpointResolutionTest(unittest.TestCase):
    def test_node_endpoint_is_used_verbatim(self):
        self.assertEqual(
            target_mod.endpoint_base_url("https://10.10.0.12:8443",
                                         fallback="http://127.0.0.1:8443"),
            "https://10.10.0.12:8443",
        )

    def test_bare_host_port_gets_the_node_scheme(self):
        self.assertEqual(
            target_mod.endpoint_base_url("10.10.0.12:8443",
                                         fallback="http://127.0.0.1:8443",
                                         default_scheme="https"),
            "https://10.10.0.12:8443",
        )

    def test_missing_endpoint_falls_back_to_global(self):
        self.assertEqual(
            target_mod.endpoint_base_url(None, fallback="http://127.0.0.1:8443",),
            "http://127.0.0.1:8443",
        )
        self.assertEqual(
            target_mod.endpoint_base_url("  ", fallback="http://127.0.0.1:8443"),
            "http://127.0.0.1:8443",
        )

    def test_executor_binds_to_the_node_endpoint_not_the_global_url(self):
        cfg = _cfg(lxd_base_url="http://127.0.0.1:1")
        ex = target_mod.executor_for_node(cfg, {"id": "n1", "lxd_endpoint": "10.10.0.12:8443"})
        self.assertEqual(ex.base_url, "https://10.10.0.12:8443")

    def test_executor_falls_back_when_node_has_no_endpoint(self):
        cfg = _cfg(lxd_base_url="http://127.0.0.1:7777")
        ex = target_mod.executor_for_node(cfg, {"id": "n1", "lxd_endpoint": None})
        self.assertEqual(ex.base_url, "http://127.0.0.1:7777")

    def test_per_node_client_cert_overrides_global(self):
        cfg = _cfg(lxd_client_cert="/global.crt", lxd_client_key="/global.key")
        node = {"id": "n1", "lxd_endpoint": "10.10.0.12:8443",
                "lxd_client_cert": "/node.crt", "lxd_client_key": "/node.key"}
        with mock.patch.object(target_mod, "LxdExecutor") as ex_cls:
            target_mod.executor_for_node(cfg, node)
        self.assertEqual(ex_cls.call_args.kwargs["client_cert"], "/node.crt")
        self.assertEqual(ex_cls.call_args.kwargs["client_key"], "/node.key")

    def test_instance_name_is_deterministic_and_lxd_safe(self):
        a = target_mod.instance_name("01M4349RQ2P5DPC7K59R0WFJCR", 0, 0)
        b = target_mod.instance_name("01M4349RQ2P5DPC7K59R0WFJCR", 0, 0)
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("termini-"))
        self.assertRegex(a, r"^[a-z0-9][a-z0-9-]*$")
        # distinct per slot so a fan-out gets distinct names
        self.assertNotEqual(a, target_mod.instance_name("01M4349RQ2P5DPC7K59R0WFJCR", 0, 1))


# --------------------------------------------------------------------------- registration

class RegistrationEndpointTest(unittest.TestCase):
    def _canonical(self, **machine_over):
        machine = {
            "machine_id": "m-a", "hostname": "node-a",
            "cpu": {"cores": 8, "threads": 8},
            "memory": {"total_bytes": 32 * GiB},
        }
        machine.update(machine_over)
        return {"schema_version": "1.0", "machine": machine}

    def test_endpoint_parsed_from_machine_lxd(self):
        reg = registration.parse_registration(
            self._canonical(lxd={"version": "5.21.8", "endpoint": "10.10.0.12:8443"}))
        self.assertEqual(reg["lxd_endpoint"], "10.10.0.12:8443")

    def test_endpoint_absent_is_none_not_an_error(self):
        reg = registration.parse_registration(self._canonical(lxd={"version": "5.21.8"}))
        self.assertIsNone(reg["lxd_endpoint"])
        reg = registration.parse_registration(self._canonical())
        self.assertIsNone(reg["lxd_endpoint"])

    def test_malformed_endpoint_rejected(self):
        from app import errors
        with self.assertRaises(errors.ApiError):
            registration.parse_registration(
                self._canonical(lxd={"endpoint": "not-an-endpoint"}))
        with self.assertRaises(errors.ApiError):
            registration.parse_registration(
                self._canonical(lxd={"endpoint": "10.10.0.12:99999"}))

    def test_legacy_shape_reports_endpoint(self):
        reg = registration.parse_registration({
            "registration_id": "node-b",
            "hardware": {"cpu": {"logical_cores": 8}, "ram_gb": 32},
            "lxd": {"endpoint": "10.10.0.13:8443"},
        })
        self.assertEqual(reg["lxd_endpoint"], "10.10.0.13:8443")


# --------------------------------------------------------------------------- apply

class ApplyPlanTest(unittest.TestCase):
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
        self.node_id = self._node("compute-1", lxd_endpoint="10.10.0.12:8443")
        self.conn.commit()

    def _node(self, reg_id, *, lxd_endpoint=None):
        reg = {
            "registration_id": reg_id, "machine_id": reg_id, "hostname": reg_id,
            "role_hint": None, "installer_version": None,
            "hardware_json": COMPUTE_HW, "os": None, "lxd": None,
            "lxd_endpoint": lxd_endpoint,
        }
        node_id, _ = store.upsert_node(
            self.conn, PROJECT_ID, reg, "compute", store.sha256(reg_id), None, store.now_iso())
        return node_id

    def _apply(self, plan, *, cfg=None, broker=None, logs=None, factory=None):
        cfg = cfg or _cfg()
        broker = broker or _Broker()
        plans = {plan["plan_id"]: plan}
        server = _ServerStub(cfg, plans, broker, logs)
        harness = _Harness(server, {"plan_id": plan["plan_id"]})
        if factory is None:
            status, body = server_mod.ControllerHandler._apply_plan(harness, self.conn, PROJECT_ID)
        else:
            with mock.patch.object(target_mod, "executor_for_node", new=factory):
                status, body = server_mod.ControllerHandler._apply_plan(
                    harness, self.conn, PROJECT_ID)
        return status, body, broker

    def test_one_instance_per_accepted_allocation_with_real_ids(self):
        plan = _plan("PLAN000000000000000000000A", [_alloc(0, self.node_id, count=1)])
        fake = _FakeExecutor()
        seen_nodes = []

        def factory(cfg, node, dry_run=False):
            seen_nodes.append(node)
            return fake

        status, body, broker = self._apply(plan, factory=factory)
        self.assertEqual(status, 202)
        self.assertEqual(body["state"], "ready")
        self.assertEqual(len(body["vm_ids"]), 1)
        vm_id = body["vm_ids"][0]
        self.assertEqual(vm_id, target_mod.instance_name(plan["plan_id"], 0, 0))
        # create called once with limits.cpu / limits.memory from the allocation
        self.assertEqual(len(fake.created), 1)
        _, kw = fake.created[0]
        self.assertEqual(kw["image_alias"], "agent-vm")
        self.assertEqual(kw["cpu"], 2)
        self.assertEqual(kw["memory"], "4GiB")
        # executor was resolved for the allocation's node
        self.assertEqual(seen_nodes[0]["id"], self.node_id)
        # terminal state published to the broker with per-allocation evidence
        ev, data, _ = broker.events[-1]
        self.assertEqual((ev, data["state"]), ("vm.provisioned", "ready"))
        self.assertEqual(data["vm_ids"], [vm_id])

    def test_count_fans_out_to_n_instances(self):
        plan = _plan("PLAN000000000000000000000B", [_alloc(0, self.node_id, count=3)])
        fake = _FakeExecutor()
        _, body, _ = self._apply(plan, factory=lambda cfg, node, dry_run=False: fake)
        self.assertEqual(body["state"], "ready")
        self.assertEqual(len(body["vm_ids"]), 3)
        self.assertEqual(len(fake.created), 3)
        names = [n for n, _ in fake.created]
        self.assertEqual(len(set(names)), 3)  # distinct names

    def test_failed_create_is_surfaced_and_releases_capacity(self):
        plan = _plan("PLAN000000000000000000000C", [_alloc(0, self.node_id, count=2)])
        # Simulate the plan-time capacity commit (controller-api.md §5.2 step 7).
        store.commit_allocation(self.conn, PROJECT_ID, plan["plan_id"], self.node_id,
                                2 * 2, 2 * 4, 2 * 8, store.now_iso())
        self.conn.commit()
        self.assertEqual(store.allocated_capacity(self.conn, self.node_id)["ram_allocated_gb"], 8)

        # First create succeeds, second fails -> the whole allocation must fail.
        first = target_mod.instance_name(plan["plan_id"], 0, 0)
        second = target_mod.instance_name(plan["plan_id"], 0, 1)
        fake = _FakeExecutor(results={second: ("error", "operation Failure: no space left")})

        _, body, broker = self._apply(plan, factory=lambda cfg, node, dry_run=False: fake)
        self.assertEqual(body["state"], "failed")
        self.assertEqual(body["vm_ids"], [])
        self.assertEqual(body["allocations"][0]["status"], "failed")
        self.assertIn("no space left", body["allocations"][0]["detail"])
        # the half-created instance was rolled back
        self.assertEqual(fake.deleted, [first])
        # capacity released: the plan no longer claims a nonexistent instance
        alloc = store.allocated_capacity(self.conn, self.node_id)
        self.assertEqual((alloc["cpu_allocated"], alloc["ram_allocated_gb"],
                          alloc["disk_allocated_gb"]), (0, 0, 0))
        self.assertEqual(broker.events[-1][1]["state"], "failed")

    def test_failed_create_is_not_reported_as_success(self):
        plan = _plan("PLAN000000000000000000000D", [_alloc(0, self.node_id, count=1)])
        fake = _FakeExecutor(default=("error", "operation still Running after 120.0s"))
        _, body, _ = self._apply(plan, factory=lambda cfg, node, dry_run=False: fake)
        self.assertEqual(body["state"], "failed")
        self.assertEqual(body["vm_ids"], [])
        self.assertNotEqual(body["state"], "provisioning")

    def test_replayed_apply_is_idempotent(self):
        plan = _plan("PLAN000000000000000000000E", [_alloc(0, self.node_id, count=1)])
        store.commit_allocation(self.conn, PROJECT_ID, plan["plan_id"], self.node_id,
                                2, 4, 8, store.now_iso())
        self.conn.commit()
        # A duplicate name returns `already_applied`, not an error.
        fake = _FakeExecutor(default="already_applied")
        _, body, _ = self._apply(plan, factory=lambda cfg, node, dry_run=False: fake)
        self.assertEqual(body["state"], "ready")
        self.assertEqual(len(body["vm_ids"]), 1)
        # capacity is untouched on a successful replay
        self.assertEqual(store.allocated_capacity(self.conn, self.node_id)["ram_allocated_gb"], 4)
        self.assertEqual(fake.deleted, [])

    def test_inference_host_service_creates_no_vm(self):
        node_id = self._node("infer-1")
        plan = _plan("PLAN000000000000000000000F",
                     [_alloc(0, node_id, realized_as="host_service")])
        fake = _FakeExecutor()
        _, body, _ = self._apply(plan, factory=lambda cfg, node, dry_run=False: fake)
        self.assertEqual(body["state"], "ready")
        self.assertEqual(body["vm_ids"], [])
        self.assertEqual(fake.created, [])
        self.assertEqual(body["allocations"][0]["realized_as"], "host_service")

    def test_unresolved_node_is_a_failure(self):
        plan = _plan("PLAN000000000000000000000G", [_alloc(0, "01J0NOSUCHNODE000000000000")])
        _, body, _ = self._apply(plan, factory=lambda cfg, node, dry_run=False: _FakeExecutor())
        self.assertEqual(body["state"], "failed")
        self.assertEqual(body["vm_ids"], [])

    def test_audit_log_records_the_outcome(self):
        plan = _plan("PLAN000000000000000000000H", [_alloc(0, self.node_id, count=1)])
        broker = _Broker()
        logs = logs_mod.OperatorLogs(broker, ring_size=100)
        fake = _FakeExecutor(default=("error", "boom"))
        self._apply(plan, broker=broker, logs=logs,
                    factory=lambda cfg, node, dry_run=False: fake)
        rows = self.conn.execute("SELECT severity, message FROM operator_logs").fetchall()
        self.assertTrue(rows)
        self.assertEqual(rows[-1]["severity"], "error")
        self.assertIn("failed", rows[-1]["message"])


# --------------------------------------------------------------------------- http end-to-end

class ApplyPlanHttpTest(unittest.TestCase):
    """Real LXD REST calls against StubLxd, proving per-node endpoint addressing."""

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
        self.lxd_a = StubLxd(); self.lxd_a.start(); self.addCleanup(self.lxd_a.stop)
        self.lxd_b = StubLxd(); self.lxd_b.start(); self.addCleanup(self.lxd_b.stop)
        self.node_a = self._node("http-a", lxd_endpoint=self.lxd_a.base_url)
        self.node_b = self._node("http-b", lxd_endpoint=self.lxd_b.base_url)
        self.conn.commit()

    def _node(self, reg_id, *, lxd_endpoint=None):
        reg = {
            "registration_id": reg_id, "machine_id": reg_id, "hostname": reg_id,
            "role_hint": None, "installer_version": None,
            "hardware_json": COMPUTE_HW, "os": None, "lxd": None,
            "lxd_endpoint": lxd_endpoint,
        }
        node_id, _ = store.upsert_node(
            self.conn, PROJECT_ID, reg, "compute", store.sha256(reg_id), None, store.now_iso())
        return node_id

    def _apply(self, plan, cfg):
        server = _ServerStub(cfg, {plan["plan_id"]: plan}, _Broker(), None)
        harness = _Harness(server, {"plan_id": plan["plan_id"]})
        return server_mod.ControllerHandler._apply_plan(harness, self.conn, PROJECT_ID)

    def test_node_endpoint_is_where_the_instance_is_created(self):
        plan = _plan("PLAN000000000000000000001A", [_alloc(0, self.node_b, count=1)])
        _, body = self._apply(plan, _cfg(lxd_base_url="http://127.0.0.1:1"))
        self.assertEqual(body["state"], "ready")
        vm_id = body["vm_ids"][0]
        self.assertIn(vm_id, self.lxd_b.instances)          # created on node B ...
        self.assertEqual(self.lxd_a.instances, {})          # ... not on node A
        self.assertEqual(len(self.lxd_a.create_requests()), 0)

    def test_node_without_endpoint_uses_the_global_fallback(self):
        node_c = self._node("http-c", lxd_endpoint=None)
        self.conn.commit()
        plan = _plan("PLAN000000000000000000001B", [_alloc(0, node_c, count=1)])
        cfg = _cfg(lxd_base_url=self.lxd_a.base_url)
        _, body = self._apply(plan, cfg)
        self.assertEqual(body["state"], "ready")
        self.assertIn(body["vm_ids"][0], self.lxd_a.instances)


if __name__ == "__main__":
    unittest.main(verbosity=2)
