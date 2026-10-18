"""Auto-placement must never prefer a seeder-fabricated node over real hardware.

MEASURED DEFECT (live run). With `console/seed_demo.py` running beside the
controller, a plan whose allocation did not name a target node (`target_node:
"auto"`) resolved to the seeder's fabricated `demo-node-a` — declared 64 GiB —
instead of the real GCE node whose facts `termini-bootstrap` actually reported
(1-8 GiB). The seeder registers through the SAME `POST /v1/nodes/register`
endpoint, with the same single-use bootstrap tokens, as hardware, so before this
change a fabricated row was indistinguishable from a registered one. The only
signal `_resolve_target` had was reported capacity, which the seeder inflates: so
`auto` deterministically put agents on a node that does not exist. The live run
only succeeded because the operator named the real node_id by hand.

This file pins the fix:
  * a node row carries explicit provenance (`synthetic`) taken from the
    registration wire (migration 0011), and the seeder declares itself;
  * `auto` selection prefers real nodes and falls back to fabricated rows ONLY when
    no real node is registered;
  * an explicit target naming a fabricated node is refused while real hardware is
    registered;
  * the resolved (real) node's capacity is what clamps, and the coarse host maxima
    come from real nodes;
  * a node-reported implausible disk — the daemon's old `size_bytes: 1` placeholder
    — is "not reported", never 0 GiB of real capacity (and the daemon now reports
    the measured root-device size).
"""
from __future__ import annotations

import importlib.util
import json
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

from app import db, health, registration, server as server_mod, store  # noqa: E402
from app.operator import target as target_mod  # noqa: E402
from app.operator.models import ExecResult  # noqa: E402
from termini.intent import DEFAULT_HOST_CAPS, clamp_validate  # noqa: E402

GiB = 1024 ** 3
PROJECT_ID = "01J0DEM0WARM000000000000AE"

# Real 26-char ULIDs (Crockford base32) so the pure-resolver cases read like the
# live fleet: SYNTH_* were the seeder's rows, REAL_* the hardware's.
SYNTH_A = "01M439D1EW2CXF9VCJJ0B6PSSQ"
SYNTH_C = "01M439JAWM38D607N5HM8K274S"
REAL_A = "01M439BZK77878M7MF5JF2XQHB"
REAL_C = "01M439DWP9ETTCS5Y8VBK0DE7Z"


# --------------------------------------------------------------------------- helpers

def hw(*, cores: int = 8, ram_gb: int = 32, disk_gb: int = 256) -> dict:
    return {
        "cpu": {"cores": cores, "threads": cores},
        "memory": {"total_bytes": ram_gb * GiB},
        "gpus": [],
        "disks": [{"size_bytes": disk_gb * GiB}],
    }


def node(nid: str, role: str = "compute", *, cores: int = 8, ram_gb: int = 32,
         disk_gb: int = 256, synthetic: bool = False) -> dict:
    return {
        "id": nid, "role": role, "synthetic": synthetic,
        "hardware_json": json.dumps(hw(cores=cores, ram_gb=ram_gb, disk_gb=disk_gb)),
    }


def spec(type_: str, *, target_node: str = "auto") -> types.SimpleNamespace:
    return types.SimpleNamespace(type=type_, target_node=target_node, passthrough_gpu=False)


class _Resolver:
    """Stand-in `self` for the pure `_resolve_target` cases."""
    _resolve_target = server_mod.ControllerHandler._resolve_target

    def resolve(self, s, nodes):
        return server_mod.ControllerHandler._resolve_target(self, s, nodes)


class _PlanHandler:
    """Stand-in `self` so `_build_plan` can be driven without a full server."""

    def _resolve_target(self, s, nodes):
        return server_mod.ControllerHandler._resolve_target(None, s, nodes)


class _CapsHandler:
    def __init__(self, project_id):
        self.server = types.SimpleNamespace(project={"id": project_id})


class _FakeExecutor:
    def __init__(self):
        self.created: list[str] = []
        self.started: list[str] = []
        self.deleted: list[str] = []

    def create_instance(self, name, **kw):
        self.created.append(name)
        return ExecResult(result="ok", method="POST", path="/1.0/instances")

    def start_instance(self, name, **kw):
        # Same contract as LxdExecutor.start_instance: a created instance is
        # STOPPED, so `_apply_plan` follows the create with a start and folds a
        # start of an already-running instance to `already_applied`.
        self.started.append(name)
        return ExecResult(result="ok", method="PUT",
                          path=f"/1.0/instances/{name}/state")

    def delete_instance(self, name, **kw):
        self.deleted.append(name)
        return ExecResult(result="ok", method="DELETE", path=f"/1.0/instances/{name}")


class _Broker:
    def __init__(self):
        self.events = []

    def publish(self, event, data, project_id):
        self.events.append((event, data, project_id))


class _ApplyHarness:
    def __init__(self, cfg, plans, broker):
        self.server = types.SimpleNamespace(cfg=cfg, plans=plans, broker=broker,
                                            operator_logs=None)
        self._plan_id = next(iter(plans))

    def _read_body(self):
        return {"plan_id": self._plan_id}

    def _require_project(self, conn, project_id):
        return None


class _DbCase(unittest.TestCase):
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

    def _node(self, reg_id: str, *, synthetic: bool, role: str = "compute",
              ram_gb: int = 32, disk_gb: int = 256, cores: int = 8) -> str:
        reg = {
            "registration_id": reg_id, "machine_id": reg_id, "hostname": reg_id,
            "role_hint": None, "installer_version": None,
            "hardware_json": hw(cores=cores, ram_gb=ram_gb, disk_gb=disk_gb),
            "os": None, "lxd": None,
            "synthetic": synthetic,
        }
        node_id, _ = store.upsert_node(
            self.conn, PROJECT_ID, reg, role, store.sha256(reg_id), None, store.now_iso())
        return node_id

    def _build(self, components):
        result = clamp_validate(components, DEFAULT_HOST_CAPS)
        return server_mod.ControllerHandler._build_plan(
            _PlanHandler(), self.conn, PROJECT_ID, result, None)


# --------------------------------------------------------------------------- 1. provenance

class RegistrationProvenanceTest(unittest.TestCase):
    """A fabricated row says so on the registration wire; hardware never does."""

    def _canonical(self, **top):
        body = {"schema_version": "1.0", "machine": {
            "machine_id": "m", "hostname": "n",
            "cpu": {"cores": 4, "threads": 4}, "memory": {"total_bytes": 8 * GiB}}}
        body.update(top)
        return body

    def test_real_node_omits_the_marker(self):
        self.assertFalse(registration.parse_registration(self._canonical())["synthetic"])

    def test_synthetic_true_is_carried(self):
        self.assertTrue(
            registration.parse_registration(self._canonical(synthetic=True))["synthetic"])

    def test_synthetic_false_is_real(self):
        self.assertFalse(
            registration.parse_registration(self._canonical(synthetic=False))["synthetic"])

    def test_registration_source_alias(self):
        self.assertTrue(registration.parse_registration(
            self._canonical(registration_source="seeder"))["synthetic"])

    def test_bogus_value_reads_as_real(self):
        # The safe reading is "real": a typo in an unrelated field must never let a
        # real node's row be quietly deprioritised.
        for junk in ("yes", 1, None, [], {"a": 1}):
            self.assertFalse(
                registration.parse_registration(
                    self._canonical(synthetic=junk))["synthetic"],
                f"synthetic={junk!r} must not read as synthetic")

    def test_legacy_shape_carries_the_marker(self):
        reg = registration.parse_registration({
            "registration_id": "demo-node-a",
            "hardware": {"cpu": {"logical_cores": 8}, "ram_gb": 64},
            "synthetic": True,
        })
        self.assertTrue(reg["synthetic"])


class StoreProvenanceTest(_DbCase):
    def test_marker_round_trips_through_the_db(self):
        synth = self._node("demo-node-a", synthetic=True)
        real = self._node("real-node", synthetic=False)
        rows = {n["id"]: n for n in store.list_nodes(self.conn, PROJECT_ID)}
        self.assertEqual(bool(rows[synth]["synthetic"]), True)
        self.assertEqual(bool(rows[real]["synthetic"]), False)

    def test_default_is_real_for_rows_that_pre_date_the_column(self):
        # Every existing caller/test builds `reg` without the key.
        nid = self._node("no-marker", synthetic=False)
        self.assertEqual(store.get_node(self.conn, nid)["synthetic"], 0)

    def test_reregistration_can_flip_synthetic_back_to_real(self):
        """A real node taking over a fabricated registration_id must clear the flag."""
        nid = self._node("demo-node-a", synthetic=True)
        self.assertEqual(store.get_node(self.conn, nid)["synthetic"], 1)
        reg = {"registration_id": "demo-node-a", "machine_id": "demo-node-a",
               "hostname": "demo-node-a", "role_hint": None, "installer_version": None,
               "hardware_json": hw(), "os": None, "lxd": None, "synthetic": False}
        nid2, created = store.upsert_node(self.conn, PROJECT_ID, reg, "compute",
                                          store.sha256("tok2"), None, store.now_iso())
        self.assertEqual(nid2, nid)
        self.assertFalse(created)
        self.assertEqual(store.get_node(self.conn, nid)["synthetic"], 0)


# --------------------------------------------------------------------------- 2. selection

class SelectionRuleTest(unittest.TestCase):
    """The measured defect, as a pure rule."""

    def setUp(self):
        self.r = _Resolver()

    def test_auto_agent_prefers_the_real_node_despite_less_ram(self):
        # The live shape: the seeder's row declares 64 GiB, the real GCE node's real
        # facts say 1 GiB. Capacity alone picked the fabricated node.
        synth = node(SYNTH_A, ram_gb=64, synthetic=True)
        real = node(REAL_A, ram_gb=1, synthetic=False)
        self.assertEqual(self.r.resolve(spec("agent_vm"), [synth, real]), real)

    def test_auto_agent_order_independent(self):
        synth = node(SYNTH_A, ram_gb=64, synthetic=True)
        real = node(REAL_A, ram_gb=1, synthetic=False)
        self.assertEqual(self.r.resolve(spec("agent_vm"), [real, synth]), real)

    def test_auto_agent_falls_back_to_the_seeder_when_nothing_real_is_registered(self):
        # The seeder-only demo must keep working: the dashboard would otherwise be
        # empty and no plan could ever be placed.
        synth = node(SYNTH_A, ram_gb=64, synthetic=True)
        self.assertEqual(self.r.resolve(spec("agent_vm"), [synth]), synth)

    def test_auto_agent_still_picks_lowest_id_among_real_nodes(self):
        lo = node(REAL_A, ram_gb=32)
        hi = node(REAL_C, ram_gb=32)
        self.assertEqual(self.r.resolve(spec("agent_vm"), [hi, lo]), lo)

    def test_auto_agent_ignores_the_synthetic_inference_row_too(self):
        synth = node(SYNTH_A, ram_gb=64, synthetic=True)
        real_c = node(REAL_C, role="inference", ram_gb=8)
        # REAL_C has less RAM than the fabricated row but is the only real candidate.
        self.assertEqual(self.r.resolve(spec("agent_vm"), [synth, real_c]), real_c)

    def test_auto_inference_prefers_the_real_inference_node(self):
        synth = node(SYNTH_C, role="inference", ram_gb=64, synthetic=True)
        real = node(REAL_C, role="inference", ram_gb=8)
        self.assertEqual(self.r.resolve(spec("inference_vm"), [synth, real]), real)

    def test_auto_inference_refuses_a_fabricated_inference_node_when_real_exists(self):
        # No real inference node: a fabricated one is not the real host service, so
        # `auto` must not resolve to it (the caller rejects invalid_target_node_type).
        synth_c = node(SYNTH_C, role="inference", ram_gb=64, synthetic=True)
        real_a = node(REAL_A, role="compute", ram_gb=8)
        self.assertIsNone(self.r.resolve(spec("inference_vm"), [synth_c, real_a]))

    def test_explicit_target_to_a_fabricated_node_is_refused(self):
        synth = node(SYNTH_A, ram_gb=64, synthetic=True)
        real = node(REAL_A, ram_gb=8)
        self.assertIsNone(
            self.r.resolve(spec("agent_vm", target_node=SYNTH_A), [synth, real]))

    def test_explicit_target_to_a_fabricated_node_allowed_when_nothing_real(self):
        synth = node(SYNTH_A, ram_gb=64, synthetic=True)
        self.assertEqual(
            self.r.resolve(spec("agent_vm", target_node=SYNTH_A), [synth]), synth)

    def test_explicit_target_to_a_real_node_still_works(self):
        synth = node(SYNTH_A, ram_gb=64, synthetic=True)
        real = node(REAL_A, ram_gb=8)
        self.assertEqual(
            self.r.resolve(spec("agent_vm", target_node=REAL_A), [synth, real]), real)

    def test_no_nodes_resolves_to_none(self):
        self.assertIsNone(self.r.resolve(spec("agent_vm"), []))


class HostCapsAuthorityTest(_DbCase):
    """Coarse host maxima come from real nodes when any real node exists."""

    def _caps(self):
        return server_mod.ControllerHandler._host_caps(
            _CapsHandler(PROJECT_ID), self.conn, DEFAULT_HOST_CAPS)

    def test_fabricated_64gib_row_cannot_widen_the_ceiling(self):
        self._node("demo-node-a", synthetic=True, ram_gb=64)
        self._node("real-node", synthetic=False, ram_gb=32)
        self.conn.commit()
        caps = self._caps()
        # 32 GiB real node -> 30; the fabricated row's 62 is ignored.
        self.assertEqual(caps["agent_vm"]["ram_gb"], 30)
        self.assertEqual(caps["agent_vm"]["vcpus"], max(16, DEFAULT_HOST_CAPS["agent_vm"]["vcpus"]))

    def test_never_tightens_below_the_frozen_default(self):
        self._node("real-node", synthetic=False, ram_gb=1, cores=2)
        self.conn.commit()
        caps = self._caps()
        self.assertEqual(caps["agent_vm"]["ram_gb"], DEFAULT_HOST_CAPS["agent_vm"]["ram_gb"])

    def test_falls_back_to_the_fabricated_rows_in_a_seeder_only_demo(self):
        self._node("demo-node-a", synthetic=True, ram_gb=64)
        self.conn.commit()
        self.assertEqual(self._caps()["agent_vm"]["ram_gb"], 62)


# --------------------------------------------------------------------------- 3. plan + capacity

class BuildPlanUsesTheRealNodeTest(_DbCase):
    def setUp(self):
        super().setUp()
        # Registration order reproduces the live defect: the fabricated row first
        # (lower ULID / earlier registered_at), so a RAM-max rule picks it.
        self.synth = self._node("demo-node-a", synthetic=True, ram_gb=64, disk_gb=512)
        self.real = self._node("real-node", synthetic=False, ram_gb=8, disk_gb=256)
        self.conn.commit()

    def test_auto_allocation_resolves_to_the_real_node(self):
        plan = self._build([{"type": "agent_vm", "count": 1, "vcpus": 2,
                             "ram_gb": 4, "disk_gb": 8}])
        self.assertEqual(plan["outcome"], "accepted", plan)
        self.assertEqual(plan["allocations"][0]["node_id"], self.real)

    def test_the_real_nodes_capacity_is_what_clamps(self):
        # 16 GiB does not fit the real node's 8 GiB (6 GiB allocatable); the
        # fabricated 64 GiB row would have accepted it unchanged.
        plan = self._build([{"type": "agent_vm", "count": 1, "vcpus": 2,
                             "ram_gb": 16, "disk_gb": 8}])
        alloc = plan["allocations"][0]
        self.assertEqual(plan["outcome"], "accepted", plan)
        self.assertEqual(alloc["node_id"], self.real)
        self.assertEqual(alloc["status"], "clamped")
        self.assertEqual(alloc["resolved"]["ram_gb"], 6)
        self.assertTrue(any(d["reason"] == "exceeds_node_ram_ceiling"
                            for d in alloc["guardrail"]["clamped_fields"]))

    def test_capacity_is_committed_against_the_real_node_only(self):
        self._build([{"type": "agent_vm", "count": 1, "vcpus": 2, "ram_gb": 4, "disk_gb": 8}])
        self.conn.commit()
        self.assertEqual(store.allocated_capacity(self.conn, self.real)["ram_allocated_gb"], 4)
        self.assertEqual(store.allocated_capacity(self.conn, self.synth)["ram_allocated_gb"], 0)

    def test_no_real_node_still_places_on_the_seeder_fallback(self):
        self.conn.execute("DELETE FROM nodes WHERE id = ?", (self.real,))
        self.conn.commit()
        plan = self._build([{"type": "agent_vm", "count": 1, "vcpus": 2,
                             "ram_gb": 4, "disk_gb": 8}])
        self.assertEqual(plan["outcome"], "accepted", plan)
        self.assertEqual(plan["allocations"][0]["node_id"], self.synth)

    def test_apply_materialises_on_the_real_node(self):
        """The plan's node_id is the only source `_apply_plan` uses, so the same
        rule decides where the LXD instance is actually created."""
        plan = self._build([{"type": "agent_vm", "count": 1, "vcpus": 2,
                             "ram_gb": 4, "disk_gb": 8}])
        fake = _FakeExecutor()
        seen: list[dict] = []
        cfg = types.SimpleNamespace(lxd_workload_image="agent-vm", lxd_create_timeout_s=5.0,
                                    inference_base_url="http://10.10.0.13:8080")
        harness = _ApplyHarness(cfg, {plan["plan_id"]: plan}, _Broker())
        with mock.patch.object(target_mod, "executor_for_node",
                               new=lambda c, n, dry_run=False: (seen.append(n), fake)[1]):
            status, body = server_mod.ControllerHandler._apply_plan(
                harness, self.conn, PROJECT_ID)
        self.assertEqual(status, 202)
        self.assertEqual(body["state"], "ready")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["id"], self.real)


# --------------------------------------------------------------------------- 4. disk accounting

class DiskAccountingTest(unittest.TestCase):
    """A nonsense disk number must never be read as real capacity — or as none."""

    def test_daemon_placeholder_is_unknown_not_zero_gib(self):
        cap = health.capacity_of({"memory": {"total_bytes": 8 * GiB},
                                  "disks": [{"size_bytes": 1}]})
        self.assertEqual(cap["disk_gb"], 0)
        self.assertFalse(cap["disk_known"])

    def test_no_disks_at_all_is_unknown(self):
        self.assertFalse(health.capacity_of({})["disk_known"])

    def test_plausible_disk_is_known_and_counted(self):
        cap = health.capacity_of({"disks": [{"size_bytes": 256 * GiB}]})
        self.assertTrue(cap["disk_known"])
        self.assertEqual(cap["disk_gb"], 256)

    def test_implausible_entries_are_excluded_from_the_sum(self):
        cap = health.capacity_of({"disks": [{"size_bytes": 1},
                                            {"size_bytes": 256 * GiB}]})
        self.assertTrue(cap["disk_known"])
        self.assertEqual(cap["disk_gb"], 256)

    def test_legacy_size_gb_is_checked_too(self):
        self.assertFalse(health.capacity_of({"disks": [{"size_gb": 0}]})["disk_known"])
        self.assertTrue(health.capacity_of({"disks": [{"size_gb": 200}]})["disk_known"])

    def test_disk_clamp_is_skipped_when_the_node_reported_no_usable_disk(self):
        cap = health.capacity_of({"cpu": {"threads": 8},
                                  "memory": {"total_bytes": 16 * GiB},
                                  "disks": [{"size_bytes": 1}]})
        resolved, decisions, reject = health.clamp_to_node_capacity(1, 1, 4, 64, cap)
        self.assertIsNone(reject)
        self.assertEqual(resolved["disk_gb"], 64)
        self.assertEqual(decisions, [])

    def test_disk_clamp_is_enforced_when_the_node_reported_a_real_disk(self):
        cap = health.capacity_of({"cpu": {"threads": 8},
                                  "memory": {"total_bytes": 16 * GiB},
                                  "disks": [{"size_bytes": 8 * GiB}]})
        resolved, decisions, reject = health.clamp_to_node_capacity(1, 1, 4, 64, cap)
        self.assertIsNone(reject)
        self.assertEqual(resolved["disk_gb"], 8)
        self.assertTrue(any(d["reason"] == "exceeds_node_disk_ceiling" for d in decisions))

    def test_hand_built_capacity_dicts_keep_the_old_behaviour(self):
        # Callers that predate `disk_known` (and pass disk_gb directly) still work.
        cap = {"ram_free_gb": 16, "cpu_free": 8, "disk_free_gb": 8, "disk_gb": 8}
        resolved, _d, reject = health.clamp_to_node_capacity(1, 1, 4, 64, cap)
        self.assertIsNone(reject)
        self.assertEqual(resolved["disk_gb"], 8)


# --------------------------------------------------------------------------- 5. the daemon

def _load_by_path(name: str, path: pathlib.Path):
    module_spec = importlib.util.spec_from_file_location(name, path)
    assert module_spec and module_spec.loader
    mod = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(mod)
    return mod


class BootstrapDiskReportingTest(unittest.TestCase):
    """The real node reported `size_bytes: 1` for its disk; it now measures it."""

    @classmethod
    def setUpClass(cls):
        cls.tb = _load_by_path("termini_bootstrap_unittest",
                               REPO / "infra" / "bootstrap" / "node" / "termini-bootstrap.py")

    def test_registration_reports_the_measured_root_device(self):
        body = self.tb.registration_body({"controller_url": "http://127.0.0.1:9"}, "unit-node")
        disks = body["machine"]["disks"]
        self.assertEqual(disks[0]["device"], "/dev/root")
        self.assertNotEqual(disks[0].get("size_bytes"), 1,
                            "the 1-byte placeholder must be gone")
        self.assertEqual(disks[0]["size_bytes"], self.tb.root_disk_bytes())
        self.assertGreaterEqual(disks[0]["size_bytes"], GiB)

    def test_the_registered_body_is_accepted_and_yields_known_disk(self):
        """The two halves agree: what the daemon sends parses to a real capacity."""
        body = self.tb.registration_body({"controller_url": "http://127.0.0.1:9"}, "unit-node")
        parsed = registration.parse_registration(body)
        self.assertFalse(parsed["synthetic"])
        self.assertGreaterEqual(parsed["disk_bytes"], GiB)
        self.assertTrue(health.capacity_of(parsed["hardware_json"])["disk_known"])

    def test_an_unmeasurable_disk_is_omitted_not_fabricated(self):
        original = self.tb.root_disk_bytes
        self.tb.root_disk_bytes = lambda: 0
        try:
            disk = self.tb.registration_disk_facts()
        finally:
            self.tb.root_disk_bytes = original
        self.assertNotIn("size_bytes", disk[0])
        self.assertEqual(health.capacity_of({"disks": disk})["disk_known"], False)


class SeederDeclaresItselfSyntheticTest(unittest.TestCase):
    def test_every_seeder_registration_is_marked_synthetic(self):
        mod = _load_by_path("seed_demo_unittest", REPO / "console" / "seed_demo.py")
        body = mod.registration_body("demo-node-a", [])
        self.assertIs(body["synthetic"], True)
        self.assertTrue(registration.parse_registration(body)["synthetic"])
        # ... and the compute + inference rows the seeder actually posts.
        self.assertIs(mod.registration_body("demo-node-c", [{"vendor": "nvidia"}])["synthetic"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
