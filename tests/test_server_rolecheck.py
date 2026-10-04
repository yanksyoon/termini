"""Unit tests for the controller's role guardrails and target resolution.

Covers architecture.md R16 / controller-api.md §5.2 step 3: `inference_vm` is a
*reference* to the always-on host inference service (realized_as host_service,
zero VMs), not a launchable VM — so it must NOT require GPU passthrough and must
resolve to the `inference` node. Also proves the canonical extractor output
("3 agents + 1 fast LLM") is accepted by the deterministic clamp→role-check
pipeline, the B5 acceptance without an LLM in the loop.
"""
from __future__ import annotations

import json
import pathlib
import sys
import types
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER = REPO / "controller"
sys.path.insert(0, str(CONTROLLER))
sys.path.insert(0, str(REPO))

from app import server as server_mod  # noqa: E402
from termini.intent import clamp_validate  # noqa: E402
from termini.intent import prompt as prompt_mod  # noqa: E402
from termini.intent.types import AllocationSpec, ValidationResult  # noqa: E402

INFERENCE_NODE = {"id": "01NODEC00000000000000000000", "role": "inference"}
COMPUTE_NODE = {"id": "01NODEA00000000000000000000", "role": "compute"}


def spec(type_: str, *, passthrough_gpu: bool = False, target_node: str = "auto"):
    return types.SimpleNamespace(
        type=type_, passthrough_gpu=passthrough_gpu, target_node=target_node
    )


class RoleCheckTest(unittest.TestCase):
    def test_inference_vm_accepts_without_passthrough_gpu(self):
        # R16: the passthrough requirement is gone; a reference to the host
        # service needs no GPU passthrough.
        ok, reason = server_mod._role_check(spec("inference_vm"), INFERENCE_NODE)
        self.assertTrue(ok, reason)
        self.assertIsNone(reason)

    def test_inference_vm_rejects_non_inference_node(self):
        ok, reason = server_mod._role_check(spec("inference_vm"), COMPUTE_NODE)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_target_node_type")

    def test_inference_vm_rejects_when_no_node_resolved(self):
        ok, reason = server_mod._role_check(spec("inference_vm"), None)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_target_node_type")

    def test_agent_vm_still_rejects_gpu_passthrough(self):
        ok, reason = server_mod._role_check(spec("agent_vm", passthrough_gpu=True), COMPUTE_NODE)
        self.assertFalse(ok)
        self.assertEqual(reason, "gpu_not_allowed_for_agent")

    def test_agent_vm_accepts_on_compute_node(self):
        ok, reason = server_mod._role_check(spec("agent_vm"), COMPUTE_NODE)
        self.assertTrue(ok, reason)

    def test_agent_vm_rejects_without_node(self):
        ok, reason = server_mod._role_check(spec("agent_vm"), None)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_target_node")


class BuildPlanSurfacesRoleReasonsTest(unittest.TestCase):
    """W7 F3 regression: role-level rejections must reach the top-level 422
    details (plan.reject_reasons), not only allocations[i].guardrail."""

    def _plan(self, alloc):
        result = ValidationResult()
        result.outcome = "accepted"
        result.allocations = [alloc]
        handler = object.__new__(server_mod.ControllerHandler)
        with mock.patch.object(
            server_mod.store, "list_nodes", return_value=[COMPUTE_NODE]
        ):
            return handler._build_plan(None, "p", result, [])

    def test_agent_vm_hostile_target_node_surfaces_in_top_level(self):
        alloc = AllocationSpec(
            type="agent_vm", count=1, vcpus=2, ram_gb=4, disk_gb=8,
            passthrough_gpu=False, target_node="controller.termini.internal",
        )
        plan = self._plan(alloc)
        self.assertEqual(plan["outcome"], "rejected")
        reasons = plan.get("reject_reasons", [])
        self.assertTrue(reasons)
        self.assertEqual(reasons[0]["index"], 0)
        self.assertEqual(reasons[0]["reason"], "invalid_target_node")
        self.assertTrue(reasons[0]["detail"])

    def test_inference_vm_wrong_node_surfaces_in_top_level(self):
        alloc = AllocationSpec(
            type="inference_vm", count=1, vcpus=8, ram_gb=16, disk_gb=64,
            passthrough_gpu=False, target_node=COMPUTE_NODE["id"],
        )
        # list_nodes returns only a compute node, so an inference_vm pinned to
        # it must resolve to a non-inference node and be rejected.
        plan = self._plan(alloc)
        self.assertEqual(plan["outcome"], "rejected")
        reasons = plan.get("reject_reasons", [])
        self.assertTrue(reasons)
        self.assertEqual(reasons[0]["reason"], "invalid_target_node_type")

    def test_valid_agent_vm_has_no_top_level_rejections(self):
        alloc = AllocationSpec(
            type="agent_vm", count=1, vcpus=2, ram_gb=4, disk_gb=8,
            passthrough_gpu=False, target_node=COMPUTE_NODE["id"],
        )
        plan = self._plan(alloc)
        self.assertEqual(plan["outcome"], "accepted")
        self.assertNotIn("reject_reasons", plan)


class ResolveTargetTest(unittest.TestCase):
    def _resolve(self, s, nodes):
        return server_mod.ControllerHandler._resolve_target(None, s, nodes)

    def test_inference_auto_resolves_to_inference_node(self):
        nodes = [COMPUTE_NODE, INFERENCE_NODE]
        self.assertEqual(self._resolve(spec("inference_vm"), nodes), INFERENCE_NODE)

    def test_inference_auto_returns_none_without_inference_node(self):
        self.assertIsNone(self._resolve(spec("inference_vm"), [COMPUTE_NODE]))

    def test_inference_explicit_target_returns_node(self):
        self.assertEqual(
            self._resolve(spec("inference_vm", target_node=INFERENCE_NODE["id"]), [INFERENCE_NODE]),
            INFERENCE_NODE,
        )

    def test_explicit_unknown_target_returns_none(self):
        self.assertIsNone(
            self._resolve(spec("inference_vm", target_node="01NOPE00000000000000000000"), [INFERENCE_NODE])
        )


class CanonicalPromptPipelineTest(unittest.TestCase):
    """The B5 acceptance: the extractor's canonical output for 'deploy 3 worker
    agents + a fast LLM for coding' passes clamp_validate AND role_check, so the
    plan is accepted (not 422)."""

    def test_canonical_few_shot_passes_clamp_and_role_check(self):
        raw = json.loads(prompt_mod.FEW_SHOT[0]["assistant"])
        result = clamp_validate(raw["allocations"], None)
        self.assertTrue(result.safe, result.to_dict())
        self.assertEqual([a.type for a in result.allocations], ["agent_vm", "inference_vm"])

        types_seen = set()
        for alloc in result.allocations:
            node = INFERENCE_NODE if alloc.type == "inference_vm" else COMPUTE_NODE
            ok, reason = server_mod._role_check(alloc, node)
            self.assertTrue(ok, f"{alloc.type}: {reason}")
            types_seen.add(alloc.type)
        self.assertEqual(types_seen, {"agent_vm", "inference_vm"})

    def test_inference_vm_false_passthrough_accepted_by_validator(self):
        result = clamp_validate(
            [{"type": "inference_vm", "count": 1, "vcpus": 8, "ram_gb": 16, "disk_gb": 64,
              "passthrough_gpu": False}],
            None,
        )
        self.assertTrue(result.safe, result.to_dict())
        self.assertEqual(result.allocations[0].type, "inference_vm")
        self.assertFalse(result.allocations[0].passthrough_gpu)


if __name__ == "__main__":
    unittest.main()
