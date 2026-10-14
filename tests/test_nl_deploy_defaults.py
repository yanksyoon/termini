"""Defect B regression: a plain natural-language deploy must fit the compute node.

Live failure this pins (measured against the standing fleet, 2026-10-14):

    POST /intents {"prompt": "deploy 2 worker agents"}
    -> 422 GUARDRAIL_REJECTED reason=exceeds_node_ram_ceiling

The guardrail was **right**: `termini-node-a` is an `n2-standard-2` (2 logical
cores, `memory.total_bytes 8319729664` ⇒ 7 GiB registered, 5 GiB allocatable
after the 2 GiB OS/LXD reserve), and the extraction prompt's few-shot showed the
model 2 vCPU / 4 GiB per agent, so the request asked for 4 vCPU / 8 GiB of
aggregate. What was wrong was the *default*: the previous workstream had to hand
specify 1 vCPU / 1 GiB per agent to make the demo run at all.

These tests hold the fix in place against node-a's real shape:

* the default an unspecified `agent_vm` gets (and that the prompt shows the
  model) fits the compute node on its own, and still fits when the demo's two
  running agents are already committed;
* the prompt's few-shot and the validator default cannot drift apart;
* a refusal names the node and the numbers, so a user can tell what to change.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER = REPO / "controller"
sys.path.insert(0, str(CONTROLLER))
sys.path.insert(0, str(REPO))

from app import db, server as server_mod, store  # noqa: E402
from termini.intent import DEFAULT_AGENT_VM, DEFAULT_HOST_CAPS, clamp_validate  # noqa: E402
from termini.intent import prompt as prompt_mod  # noqa: E402

PROJECT_ID = "01J0DEM0WARM000000000000AD"

# `termini-node-a`'s registered hardware facts, read from the deployed controller
# (`GET /v1/projects/.../nodes`): 2 cores / 2 threads, 8,319,729,664 bytes RAM,
# 61,285,326,848-byte boot disk. capacity_of -> cpu_cores 2, ram_gb 7, disk_gb 57
# -> cpu_free 4 (2.0 x 2 overcommit), ram_free_gb 5 (7 - 2 reserve), disk 52.
NODE_A_HW = {
    "cpu": {"model": "Intel(R) Xeon(R) CPU @ 2.80GHz", "cores": 2, "threads": 2},
    "memory": {"total_bytes": 8319729664},
    "gpus": [],
    "disks": [{"device": "/dev/root", "type": "ssd", "health": "ok",
               "size_bytes": 61285326848}],
}


def default_agent_request(count: int = 2) -> list[dict]:
    """What "deploy N worker agents" must resolve to with no sizes named."""
    return [{"type": "agent_vm", "count": count, **DEFAULT_AGENT_VM,
             "passthrough_gpu": False, "target_node": "auto"}]


class _Handler:
    """Stand-in `self` so `_build_plan` can be driven without a full server."""

    def _resolve_target(self, spec, nodes):
        return server_mod.ControllerHandler._resolve_target(None, spec, nodes)


class _PlanBase(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.addCleanup(self._rm)
        db.migrate(self.path)
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "INSERT INTO projects (id, name, project_token_hash, max_inference_nodes, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (PROJECT_ID, "test", "a" * 64, 1, store.now_iso()),
        )
        reg = {
            "registration_id": "node-a", "machine_id": "node-a", "hostname": "node-a",
            "role_hint": None, "installer_version": None,
            "hardware_json": NODE_A_HW, "os": None, "lxd": None,
        }
        self.node_id, _ = store.upsert_node(
            self.conn, PROJECT_ID, reg, "compute", store.sha256("node-a"),
            None, store.now_iso(),
        )
        self.conn.commit()

    def _rm(self):
        import os
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _build(self, components):
        result = clamp_validate(components, DEFAULT_HOST_CAPS)
        return server_mod.ControllerHandler._build_plan(
            _Handler(), self.conn, PROJECT_ID, result, None,
        )

    def _commit(self, cpu, ram, disk, plan_id="PLAN000000000000000000000A"):
        store.commit_allocation(self.conn, PROJECT_ID, plan_id, self.node_id,
                                cpu, ram, disk, store.now_iso())
        self.conn.commit()


class DefaultAgentSizeFitsComputeNodeTest(_PlanBase):
    def test_the_documented_default_is_sized_for_the_compute_node(self):
        # 1 vCPU / 1 GiB / 8 GiB per agent x 2 = 2 vCPU / 2 GiB, against 4 vCPU
        # and 5 GiB allocatable: it fits with headroom.
        self.assertEqual(DEFAULT_AGENT_VM, {"vcpus": 1, "ram_gb": 1, "disk_gb": 8})

    def test_plain_nl_deploy_is_accepted_on_an_empty_compute_node(self):
        plan = self._build(default_agent_request(2))
        self.assertEqual(plan["outcome"], "accepted", plan.get("reject_reasons"))
        alloc = plan["allocations"][0]
        self.assertEqual(alloc["status"], "accepted")
        self.assertEqual(alloc["node_id"], self.node_id)
        self.assertGreaterEqual(alloc["resolved"]["ram_gb"], 1)
        self.assertGreaterEqual(alloc["resolved"]["vcpus"], 1)

    def test_plain_nl_deploy_still_fits_alongside_the_demo_agents(self):
        """The live fleet state: two demo agents already committed on node-a.

        `cpu_allocated 2 / ram_allocated_gb 2` is the previous workstream's
        2 x (1 vCPU / 1 GiB) plan. Free becomes 2 vCPU / 3 GiB, and the default
        two-agent deploy still fits (`free_ram // count == 1`).
        """
        self._commit(cpu=2, ram=2, disk=8)
        plan = self._build(default_agent_request(2))
        self.assertEqual(plan["outcome"], "accepted", plan.get("reject_reasons"))
        self.assertIn(plan["allocations"][0]["status"], ("accepted", "clamped"))

    def test_the_old_generous_default_saturated_node_a_after_one_deploy(self):
        """Why the DEFAULT matters, not just the clamp.

        The per-VM clamp only rescues a request while `free_ram // count >= 1`.
        The old 2 vCPU / 4 GiB default asked for 8 GiB on a node with 5 GiB free,
        so the clamp halved it to 2 GiB *and committed the whole 5 GiB of
        headroom* (2 x 2 GiB, rounded into the 4 vCPU ceiling too). Node-a was
        then full: the identical second request had no per-VM share left and was
        REFUSED. The fixed 1 vCPU / 1 GiB default commits 2 GiB, so the demo can
        be deployed again — which is what a judge does.
        """
        old = [{"type": "agent_vm", "count": 2, "vcpus": 2, "ram_gb": 4, "disk_gb": 8,
                "passthrough_gpu": False, "target_node": "auto"}]
        first = self._build(old)
        self.assertEqual(first["outcome"], "accepted", first.get("reject_reasons"))
        # The clamp shrank each agent, but the reservation took all the headroom.
        self.assertEqual(first["allocations"][0]["resolved"]["ram_gb"], 2)

        second = self._build(old)
        self.assertEqual(second["outcome"], "rejected",
                         "the old default left node-a unable to host a second deploy")
        self.assertIn("exceeds_node_ram_ceiling",
                      [r["reason"] for r in second["reject_reasons"]])

    def test_the_default_keeps_node_a_repeatable(self):
        first = self._build(default_agent_request(2))
        second = self._build(default_agent_request(2))
        self.assertEqual(first["outcome"], "accepted", first.get("reject_reasons"))
        self.assertEqual(second["outcome"], "accepted",
                         "the default must leave room for the next deploy")
        self.assertEqual(first["allocations"][0]["resolved"]["ram_gb"],
                         DEFAULT_AGENT_VM["ram_gb"])

    def test_refusal_names_the_node_and_the_numbers(self):
        self._commit(cpu=4, ram=4, disk=40)  # node-a fully committed
        plan = self._build(default_agent_request(2))
        self.assertEqual(plan["outcome"], "rejected")
        detail = plan["reject_reasons"][0]["detail"]
        self.assertIn(self.node_id, detail)
        self.assertIn("ram_free_gb=", detail)


class PromptDefaultCannotDriftTest(unittest.TestCase):
    def test_few_shot_agent_size_is_the_documented_default(self):
        raw = json.loads(prompt_mod.FEW_SHOT[0]["assistant"])
        agent = next(a for a in raw["allocations"] if a["type"] == "agent_vm")
        for field, value in DEFAULT_AGENT_VM.items():
            self.assertEqual(agent[field], value,
                             f"prompt few-shot {field} drifted from DEFAULT_AGENT_VM")

    def test_system_prompt_states_the_default(self):
        for field, value in DEFAULT_AGENT_VM.items():
            self.assertIn(str(value), prompt_mod.SYSTEM_PROMPT,
                          f"the system prompt must state the {field} default")


if __name__ == "__main__":
    unittest.main(verbosity=2)
