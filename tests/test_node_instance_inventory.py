"""`lxd_instances[]` and `services[]` from a real node's own LXD (t_8b66366d).

Before this change the baked node daemon collected NO `lxd_instances` at all, and
filled `services[]` only when the node happened to run the inference front door —
so every real node reported `lxd_instances = None` / `services = None`. The
operator's guardrail resolves a model-chosen remediation target against
`lxd_instances[]`, so with the field absent a decision on a REAL fault could never
resolve to a real instance (the demo's decisions fell back or targeted the
seeder's synthetic ids and 404'd at the node).

These tests pin the collection itself: the id derivation (byte-compatible with the
controller's and the seeder's copies), the mapping of a real LXD `recursion=2`
inventory onto the contract's §4.6 fields, the omission rules, and that the
emitted sample still passes the controller's ingest validator.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import re
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))

from app import fields as F            # noqa: E402
from app.validate import validate_sample  # noqa: E402


def _load_bootstrap():
    path = REPO / "infra" / "bootstrap" / "node" / "termini-bootstrap.py"
    spec = importlib.util.spec_from_file_location("termini_bootstrap_instances", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tb = _load_bootstrap()


def _instance(name, status="Running", usage=1_000_000_000, total=4_294_967_296,
              disk_usage=2_362_232_012, cpu_ns=0, itype="container"):
    """One `GET /1.0/instances?recursion=2` element, shaped as LXD returns it."""
    return {
        "name": name, "status": status, "type": itype,
        "expanded_config": {"limits.memory": "4GiB"} if total else {},
        "state": {
            "status": status,
            "cpu": {"usage": cpu_ns},
            "memory": {"usage": usage, "total": total},
            "disk": {"root": {"usage": disk_usage, "total": 0}},
        },
    }


class VmIdDerivationTest(unittest.TestCase):
    """The reported id is derived from the instance name, deterministically.

    `vm_id` is a C0 id and the ingest validator requires a 26-char Crockford ULID,
    while an LXD instance name is lowercase and may carry customer hostname data —
    so the name is never sent and the id is derived instead. The controller
    (`controller/app/operator/vmids.py`) and the demo seeder carry the same rule;
    these literals are what makes a drift between the copies fail the build
    instead of silently making every remediation unresolvable.
    """

    PINNED = {
        "demo-workload": "NNB9287WRHGFDPEZ995TKCXDNW",
        "termini-01m4wjhwqr3b8pcav2tvqbgg-0-0": "7NM4JV8BSD1P8KYBYP8B7BT7CY",
        "termini-01m4wjhwqr3b8pcav2tvqbgg-0-1": "VW4XZD24288MCXGT4557CDYFF9",
    }

    def test_pinned_literals(self):
        for name, expected in self.PINNED.items():
            self.assertEqual(tb.vm_id_for_instance(name), expected)

    def test_ids_are_valid_controller_ulids(self):
        for name in self.PINNED:
            vm_id = tb.vm_id_for_instance(name)
            self.assertEqual(len(vm_id), 26)
            self.assertRegex(vm_id, re.compile(F.ULID_PATTERN))

    def test_deterministic_and_one_way(self):
        self.assertEqual(tb.vm_id_for_instance("agent-c1"),
                         tb.vm_id_for_instance("agent-c1"))
        self.assertNotEqual(tb.vm_id_for_instance("agent-c1"),
                            tb.vm_id_for_instance("agent-c2"))
        # the name (customer data) never appears in the reported id
        self.assertNotIn("workload", tb.vm_id_for_instance("demo-workload").lower())

    def test_spec_field_is_ulid_typed(self):
        # if the contract's id format ever changes, this test is the tripwire
        self.assertTrue(F.LXD_INSTANCE_FIELDS["vm_id"].get("ulid"))


class InventoryTest(unittest.TestCase):
    """A real LXD inventory maps onto the contract fields (§4.6)."""

    def setUp(self):
        tb._CPU_PREV.clear()
        self.original = tb.lxd_api_get

    def tearDown(self):
        tb.lxd_api_get = self.original
        tb._CPU_PREV.clear()

    def _payload(self, items):
        tb.lxd_api_get = lambda path, timeout=None: (
            items if path == "/1.0/instances?recursion=2" else None)

    def test_running_instances_only(self):
        self._payload([
            _instance("agent-0"),
            _instance("demo-workload", status="Stopped"),
            _instance("agent-1", itype="virtual-machine"),
        ])
        out = tb.lxd_instance_inventory({})
        self.assertEqual([e["vm_id"] for e in out],
                         [tb.vm_id_for_instance("agent-0"),
                          tb.vm_id_for_instance("agent-1")])
        self.assertEqual({e["state"] for e in out}, {"running"})

    def test_contract_fields_come_from_lxd(self):
        self._payload([_instance("agent-0", usage=3_328_599_654, total=4_294_967_296,
                                 disk_usage=2_362_232_012)])
        entry = tb.lxd_instance_inventory({})[0]
        self.assertEqual(entry["ram_used_bytes"], 3_328_599_654)
        self.assertEqual(entry["ram_limit_bytes"], 4_294_967_296)
        self.assertEqual(entry["disk_used_bytes"], 2_362_232_012)
        self.assertEqual(entry["state"], "running")

    def test_first_sample_omits_cpu_then_reports_the_delta(self):
        payload = [_instance("agent-0", cpu_ns=1_000_000_000)]
        self._payload(payload)
        first = tb.lxd_instance_inventory({})[0]
        self.assertNotIn("cpu_util_pct", first)      # no delta yet: not a fake 0.0
        payload[0]["state"]["cpu"]["usage"] = 1_000_000_000 + 500_000_000
        second = tb.lxd_instance_inventory({})[0]
        self.assertIn("cpu_util_pct", second)
        self.assertGreater(second["cpu_util_pct"], 0.0)
        self.assertLessEqual(second["cpu_util_pct"], 100.0)

    def test_counter_reset_is_not_reported_as_negative(self):
        payload = [_instance("agent-0", cpu_ns=9_000_000_000)]
        self._payload(payload)
        tb.lxd_instance_inventory({})
        payload[0]["state"]["cpu"]["usage"] = 1
        self.assertNotIn("cpu_util_pct", tb.lxd_instance_inventory({})[0])

    def test_stopped_instance_drops_its_cpu_history(self):
        self._payload([_instance("agent-0", cpu_ns=1)])
        tb.lxd_instance_inventory({})
        self.assertIn("agent-0", tb._CPU_PREV)
        self._payload([])
        tb.lxd_instance_inventory({})
        self.assertEqual(tb._CPU_PREV, {})

    def test_unreadable_lxd_is_none_not_an_empty_inventory(self):
        # an empty list IS a legitimate answer (a node running nothing); a junk
        # response is not, and must be reported as "no inventory this sample"
        for broken in (None, "nonsense", {"metadata": []}, 42):
            tb.lxd_api_get = lambda path, timeout=None, _b=broken: _b
            self.assertIsNone(tb.lxd_instance_inventory({}),
                              "an unreadable LXD must be reported as 'no inventory'")

    def test_junk_entries_are_skipped_not_fatal(self):
        self._payload(["not-a-dict", {}, {"name": ""}, {"name": "ok", "state": "garbage"},
                       {"name": "no-state", "status": "Running"}])
        out = tb.lxd_instance_inventory({})
        self.assertEqual([e["state"] for e in out], ["running"])
        self.assertEqual(out[0]["ram_used_bytes"], 0)
        self.assertEqual(out[0]["ram_limit_bytes"], 0)

    def test_config_can_disable_collection(self):
        tb.lxd_api_get = lambda path, timeout=None: self.fail("LXD was queried")
        self.assertIsNone(tb.lxd_instance_inventory({"lxd_inventory": "0"}))


class ServiceFactsTest(unittest.TestCase):
    """`services[]` is emitted on every sample, from a fixed allowlist (§4.8)."""

    def setUp(self):
        # The seam is `_unit_facts` since t_957956e5 (it returns the operational
        # fields alongside the status), and journal counts are a separate probe
        # that would otherwise shell out to journalctl from a unit test.
        self.original = tb._unit_facts
        tb._unit_facts = lambda unit: {"status": {
            "snap.lxd.daemon.service": "running",
            "ssh.service": "running",
        }.get(unit)} if unit in ("snap.lxd.daemon.service", "ssh.service") else None
        self.original_journal = tb.journal_counts
        tb.journal_counts = lambda unit: {}

    def tearDown(self):
        tb._unit_facts = self.original
        tb.journal_counts = self.original_journal

    def test_compute_node_reports_its_services(self):
        out = tb.service_facts({"role_hint": "compute"}, None)
        self.assertEqual(out, [{"name": "termini-bootstrap", "status": "running"},
                               {"name": "lxd", "status": "running"},
                               {"name": "sshd", "status": "running"}])

    def test_names_stay_inside_the_contract_allowlist(self):
        allowed = set(F.SERVICE_FIELDS["name"]["enum"])
        for cfg, inf in (({"role_hint": "compute"}, None),
                         ({"role_hint": "inference"}, {"server_up": True}),
                         ({"role_hint": "inference"}, {"server_up": False})):
            for entry in tb.service_facts(cfg, inf):
                self.assertIn(entry["name"], allowed)
                self.assertIn(entry["status"], F.SERVICE_FIELDS["status"]["enum"])

    def test_inference_node_reports_llama_cpp(self):
        up = tb.service_facts({"role_hint": "inference"}, {"server_up": True})
        self.assertEqual(up[-1], {"name": "llama-cpp", "status": "running"})
        down = tb.service_facts({"role_hint": "inference"}, {"server_up": False})
        self.assertEqual(down[-1], {"name": "llama-cpp", "status": "stopped"})

    def test_stopped_unit_is_reported_as_stopped(self):
        tb._unit_facts = lambda unit: {"status": {
            "ssh.service": "stopped",
            "snap.lxd.daemon.service": "failed",
        }.get(unit)} if unit in ("ssh.service", "snap.lxd.daemon.service") else None
        out = tb.service_facts({"role_hint": "compute"}, None)
        self.assertIn({"name": "lxd", "status": "failed"}, out)
        self.assertIn({"name": "sshd", "status": "stopped"}, out)


class BuildSampleTest(unittest.TestCase):
    """The sample a node actually sends carries both fields and stays valid."""

    def setUp(self):
        self.api, self.unit = tb.lxd_api_get, tb._unit_facts
        self.journal = tb.journal_counts
        tb.lxd_api_get = lambda path, timeout=None: [_instance("agent-0")] if "instances" in path else None
        tb._unit_facts = lambda unit: {"status": "running"} if unit in (
            "snap.lxd.daemon.service", "ssh.service") else None
        tb.journal_counts = lambda unit: {}

    def tearDown(self):
        tb.lxd_api_get, tb._unit_facts = self.api, self.unit
        tb.journal_counts = self.journal
        tb._CPU_PREV.clear()

    def _cfg(self):
        return {"controller_url": "http://127.0.0.1:9", "node_name": "test-node",
                "iface": "eth0", "role_hint": "compute"}

    def test_sample_validates_and_carries_the_inventory(self):
        sample = tb.build_sample(self._cfg(), 1)
        self.assertIn("lxd_instances", sample)
        self.assertEqual(sample["lxd_instances"][0]["vm_id"],
                         tb.vm_id_for_instance("agent-0"))
        self.assertTrue(sample["services"])
        _clean, errors = validate_sample(sample)
        self.assertEqual(errors, [])

    def test_sample_omits_the_field_without_lxd_and_still_validates(self):
        tb.lxd_api_get = lambda path, timeout=None: None
        sample = tb.build_sample(self._cfg(), 1)
        self.assertNotIn("lxd_instances", sample)
        self.assertTrue(sample["services"])
        _clean, errors = validate_sample(sample)
        self.assertEqual(errors, [])


class VerifyToolTest(unittest.TestCase):
    """The live-node acceptance tool (`infra/gcp/tools/verify-node-instances.py`).

    Its whole value is that it FAILS when the data is not there, so the failure
    paths are what is pinned here.
    """

    @classmethod
    def setUpClass(cls):
        path = REPO / "infra" / "gcp" / "tools" / "verify-node-instances.py"
        loader = importlib.machinery.SourceFileLoader("verify_node_instances", str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.tool = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.tool)

    def setUp(self):
        self.vm_id = tb.vm_id_for_instance("agent-0")
        self.expected = {self.vm_id: "agent-0"}
        self.services = [{"name": "termini-bootstrap", "status": "running"}]

    def _sample(self, instances, services=None):
        s = {"seq": 1}
        if instances is not None:
            s["lxd_instances"] = instances
        if services is not None:
            s["services"] = services
        return s

    def test_matching_sample_passes(self):
        sample = self._sample([{"vm_id": self.vm_id, "state": "running",
                                "ram_used_bytes": 1, "ram_limit_bytes": 2,
                                "disk_used_bytes": 3}], self.services)
        self.assertEqual(self.tool.compare(self.expected, sample), [])

    def test_absent_field_fails(self):
        reasons = self.tool.compare(self.expected, self._sample(None, self.services))
        self.assertTrue(any("absent" in r for r in reasons), reasons)

    def test_running_instance_missing_from_the_sample_fails(self):
        reasons = self.tool.compare(self.expected, self._sample([], self.services))
        self.assertTrue(any("not in the stored sample" in r for r in reasons), reasons)

    def test_invented_or_stopped_instance_fails(self):
        invented = self._sample([{"vm_id": "01M4N827979BD9D2762EFE24A0", "state": "running"}],
                                self.services)
        reasons = self.tool.compare(self.expected, invented)
        self.assertTrue(any("not a running instance" in r for r in reasons), reasons)

        stopped = self._sample([{"vm_id": self.vm_id, "state": "stopped"}], self.services)
        reasons = self.tool.compare(self.expected, stopped)
        self.assertTrue(any("state='stopped'" in r for r in reasons), reasons)

    def test_empty_services_fails(self):
        sample = self._sample([{"vm_id": self.vm_id, "state": "running"}], [])
        reasons = self.tool.compare(self.expected, sample)
        self.assertTrue(any("services[]" in r for r in reasons), reasons)


if __name__ == "__main__":
    unittest.main()
