#!/usr/bin/env python3
"""One id derivation, three copies — and they must agree byte for byte.

`lxd_instances[].vm_id` is how a node names its workloads to the controller, and
it is how the controller (and the demo seeder) resolve a remediation target back
to a real LXD instance. The derivation lives in three places because the three
run in different processes and neither may import the other:

  * ``controller/app/operator/vmids.py``      (the controller: resolve)
  * ``infra/bootstrap/node/termini-bootstrap.py``  (the node: report)
  * ``console/seed_demo.py``                  (the demo seeder: synthesize)

A drift between them is silent and terminal: the controller would hash a
reported vm_id from names that do not produce it, the guardrail would keep the
vm_id as the instance name, and the executor would refuse every remediation
(`target_missing`) — i.e. the demo would break with no error anywhere. So the
agreement is asserted here rather than left to a comment.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))

from app.operator import vmids  # noqa: E402
from app import fields as F      # noqa: E402


def _load(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


NODE_AGENT = _load(REPO / "infra" / "bootstrap" / "node" / "termini-bootstrap.py",
                   "termini_bootstrap_under_test")
SEEDER = _load(REPO / "console" / "seed_demo.py", "seed_demo_under_test")

NAMES = [
    "demo-workload",
    "termini-01m4wjhwqr3b8pcav2tvqbgg-0-0",
    "termini-01m4wjhwqr3b8pcav2tvqbgg-0-1",
    "agent-c1",
    "a",
    "x" * 200,
]


class DerivationTest(unittest.TestCase):
    def test_all_three_implementations_agree(self):
        for name in NAMES:
            controller = vmids.vm_id_for_instance(name)
            self.assertEqual(controller, NODE_AGENT.vm_id_for_instance(name),
                             f"node agent drifts from the controller for {name!r}")
            self.assertEqual(controller, SEEDER.vm_id_for_instance(name),
                             f"seeder drifts from the controller for {name!r}")

    def test_ids_are_c0_ulid_shaped_and_do_not_leak_the_name(self):
        for name in NAMES:
            vm_id = vmids.vm_id_for_instance(name)
            self.assertEqual(len(vm_id), 26)
            self.assertRegex(vm_id, F.ULID_PATTERN)   # the ingest validator's rule
            if len(name) <= 26:
                self.assertNotEqual(vm_id, name.upper())

    def test_distinct_names_give_distinct_ids(self):
        ids = [vmids.vm_id_for_instance(n) for n in NAMES]
        self.assertEqual(len(set(ids)), len(NAMES))

    def test_mapping_round_trips_through_the_inventory(self):
        mapping = vmids.instance_names_for_vm_ids(["agent-c1", "demo-workload"])
        for name in ("agent-c1", "demo-workload"):
            self.assertEqual(mapping[vmids.vm_id_for_instance(name)], name)

    def test_seeder_advertises_the_derived_id_only_for_a_real_instance(self):
        derived = SEEDER.vm_id_for_instance("agent-c1")
        self.assertEqual(SEEDER.advertised_vm_ids("node-1", ["agent-c1"], False), [derived])
        # No instance given, or explicitly synthetic: the historical invented id,
        # which the executor refuses (that is the point of the flag).
        fake = SEEDER.advertised_vm_ids("node-1", [], False)
        self.assertEqual(fake, [SEEDER.instance_vm_id("node-1")])
        self.assertNotEqual(fake[0], derived)
        self.assertEqual(SEEDER.advertised_vm_ids("node-1", ["agent-c1"], True), fake)


if __name__ == "__main__":
    unittest.main()
