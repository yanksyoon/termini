"""`inference_vm` resolves to the host of the agent-facing inference FRONT DOOR.

MEASURED DEFECT (t_d7e0bf4e, live standing fleet 2026-10-17). The demo's headline
prompt — `deploy 3 worker agents for coding plus a fast LLM` — was a guaranteed
`422 GUARDRAIL_REJECTED`:

    {"index":1,"reason":"invalid_target_node_type",
     "detail":"inference_vm must resolve to the inference node"}

The frozen §4.1 role rule assigns `inference` only to a node reporting a GPU with
`vram_bytes >= 12 GiB`. After the CPU/proxy pivot (infra-decisions.md D9) the fleet
has no GPU at all, so no node could carry that role: every `inference_vm` in every
prompt was unrunnable, while the very node the reference class names — node-c,
`10.10.0.13:8080`, `backend=gemini-proxy` — was registered and answering.

The fix keeps the frozen ROLE rule and widens the RESOLUTION rule for the
reference class (recorded as a D9 amendment, controller-api.md §5.2 step 3):

1. a real node the role rule assigned `inference` (unchanged, still first);
2. else the REAL node that serves the agent-facing inference front door — the
   host of `TERMINI_INFERENCE_BASE_URL` (plus operator-named hosts), the same
   address §5.2 step 5 already uses to keep *auto* agent placement off the model
   host;
3. else `None` -> `invalid_target_node_type` (a genuinely wrong target still
   fails, and its detail says what was looked for).

`realized_as` stays `host_service`: no VM is created for the reference, no GPU is
claimed, and a seeder-fabricated row is never resurrected as an inference node.
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

GiB = 1024 ** 3

# The live fleet's shapes (node-a 2 vCPU/8 GiB, node-c 8 vCPU/31 GiB, both CPU-only),
# with real 26-char ULIDs so the cases read like the fleet.
NODE_A = "01M4TGD31WXH97JAA5S6A4R10D"
NODE_C = "01M4ZQ7Q2J80ATY3K32PY3PPJH"
SYNTH_C = "01M4NZK474BVARFBXYQBMVA8GV"

NODE_A_HOST = "10.10.0.11"
NODE_C_HOST = "10.10.0.13"
FRONT_DOOR = f"http://{NODE_C_HOST}:8080"


def hw(*, cores: int = 2, ram_gb: int = 7, disk_gb: int = 57) -> dict:
    return {
        "cpu": {"cores": cores, "threads": cores},
        "memory": {"total_bytes": ram_gb * GiB},
        "gpus": [],
        "disks": [{"size_bytes": disk_gb * GiB}],
    }


def node(nid: str, *, role: str = "compute", host: str = "", synthetic: bool = False,
         cores: int = 2, ram_gb: int = 7, disk_gb: int = 57) -> dict:
    return {
        "id": nid,
        "role": role,
        "synthetic": synthetic,
        "lxd_endpoint": f"https://{host}:8443" if host else None,
        "source_ip": host or None,
        "hardware_json": json.dumps(hw(cores=cores, ram_gb=ram_gb, disk_gb=disk_gb)),
    }


REAL_A = node(NODE_A, host=NODE_A_HOST)
REAL_C = node(NODE_C, host=NODE_C_HOST, cores=8, ram_gb=31, disk_gb=57)
GPU_C = node(NODE_C, role="inference", host=NODE_C_HOST, cores=8, ram_gb=31)


def spec(type_: str, *, target_node: str = "auto") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        type=type_, target_node=target_node, passthrough_gpu=False,
        count=1, vcpus=4, ram_gb=8, disk_gb=32,
        to_dict=lambda: {"type": type_, "count": 1, "vcpus": 4, "ram_gb": 8,
                         "disk_gb": 32, "target_node": target_node},
    )


class _Resolver:
    """Stand-in `self` for the pure `_resolve_target` cases."""

    def __init__(self, inference_base_url: str = FRONT_DOOR, exclude: tuple = ()):
        self.server = types.SimpleNamespace(cfg=types.SimpleNamespace(
            inference_base_url=inference_base_url,
            agent_placement_exclude_hosts=tuple(exclude),
        ))

    def resolve(self, s, nodes):
        return server_mod.ControllerHandler._resolve_target(self, s, nodes)


class InferenceServiceHostResolutionTest(unittest.TestCase):
    """Step 2 of §5.2 step 3: the front-door host, not a GPU role."""

    def test_auto_resolves_to_the_node_serving_the_front_door(self):
        self.assertEqual(_Resolver().resolve(spec("inference_vm"), [REAL_A, REAL_C]), REAL_C)

    def test_auto_still_prefers_a_real_inference_role_node(self):
        # If the fleet ever gets a GPU node again, the frozen §4.1 rule wins.
        self.assertEqual(_Resolver().resolve(spec("inference_vm"), [GPU_C, REAL_A]), GPU_C)

    def test_auto_prefers_inference_role_over_the_front_door_host(self):
        # node-a serves the front door here; the GPU node still wins — role first.
        r = _Resolver(inference_base_url=f"http://{NODE_A_HOST}:8080")
        self.assertEqual(r.resolve(spec("inference_vm"), [GPU_C, REAL_A]), GPU_C)

    def test_auto_rejects_when_no_node_serves_the_front_door(self):
        # The address names no registered node -> no resolvable host service.
        r = _Resolver(inference_base_url="http://10.99.0.99:8080")
        self.assertIsNone(r.resolve(spec("inference_vm"), [REAL_A, REAL_C]))

    def test_auto_rejects_when_no_front_door_is_configured(self):
        # "no inference node registered" still rejects: nothing to resolve to.
        self.assertIsNone(_Resolver(inference_base_url="").resolve(
            spec("inference_vm"), [REAL_A, REAL_C]))

    def test_auto_uses_an_operator_named_front_door_host(self):
        r = _Resolver(inference_base_url="", exclude=(NODE_C_HOST,))
        self.assertEqual(r.resolve(spec("inference_vm"), [REAL_A, REAL_C]), REAL_C)

    def test_a_synthetic_row_is_never_the_front_door_host(self):
        # The seeder's fabricated row matches the address but is not hardware:
        # the reference class names the fleet's REAL model host (provenance rule).
        synth = node(SYNTH_C, host=NODE_C_HOST, synthetic=True, cores=8, ram_gb=64)
        self.assertIsNone(_Resolver().resolve(spec("inference_vm"), [REAL_A, synth]))

    def test_explicit_target_naming_the_front_door_host_resolves(self):
        self.assertEqual(
            _Resolver().resolve(spec("inference_vm", target_node=NODE_C), [REAL_A, REAL_C]),
            REAL_C)

    def test_explicit_target_naming_an_unrelated_compute_node_resolves_to_it(self):
        # Resolution is not the guardrail: the role check below still refuses this
        # (node-a is neither an inference node nor the front-door host).
        self.assertEqual(
            _Resolver().resolve(spec("inference_vm", target_node=NODE_A), [REAL_A, REAL_C]),
            REAL_A)


class RoleCheckServiceHostTest(unittest.TestCase):
    def test_service_host_is_accepted_without_a_gpu_role(self):
        ok, reason = server_mod._role_check(spec("inference_vm"), REAL_C, service_host=True)
        self.assertTrue(ok, reason)
        self.assertIsNone(reason)

    def test_compute_node_that_is_not_the_front_door_is_still_rejected(self):
        ok, reason = server_mod._role_check(spec("inference_vm"), REAL_A)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_target_node_type")

    def test_no_node_is_still_rejected(self):
        ok, reason = server_mod._role_check(spec("inference_vm"), None)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_target_node_type")


class HeadlinePromptPlanTest(unittest.TestCase):
    """The exact demo prompt, end to end through clamp -> role check -> plan."""

    RAW = [
        {"type": "agent_vm", "count": 3, "vcpus": 1, "ram_gb": 1, "disk_gb": 4},
        {"type": "inference_vm", "count": 1, "vcpus": 4, "ram_gb": 8, "disk_gb": 32},
    ]

    def _handler(self, base_url: str = FRONT_DOOR):
        h = object.__new__(server_mod.ControllerHandler)
        h.server = types.SimpleNamespace(cfg=types.SimpleNamespace(
            inference_base_url=base_url, agent_placement_exclude_hosts=(),
        ))
        return h

    def _plan(self, nodes, raw=None, base_url: str = FRONT_DOOR):
        result = clamp_validate(raw or self.RAW, None)
        self.assertTrue(result.safe, result.to_dict())
        with mock.patch.object(server_mod.store, "list_nodes", return_value=nodes), \
             mock.patch.object(server_mod.store, "commit_allocation") as commit:
            plan = self._handler(base_url)._build_plan(None, "p", result, raw or self.RAW)
        return plan, commit

    def test_headline_prompt_is_accepted(self):
        plan, _ = self._plan([REAL_A, REAL_C])
        self.assertEqual(plan["outcome"], "accepted")
        self.assertNotIn("reject_reasons", plan)

    def test_inference_half_is_a_host_service_reference(self):
        plan, _ = self._plan([REAL_A, REAL_C])
        inf = plan["allocations"][1]
        self.assertEqual(inf["status"], "accepted")
        self.assertEqual(inf["node_id"], NODE_C)
        self.assertEqual(inf["realized_as"], "host_service")
        self.assertEqual(inf["vm_ids"], [])

    def test_agent_half_lands_on_the_compute_node_and_commits_capacity(self):
        plan, commit = self._plan([REAL_A, REAL_C])
        agent = plan["allocations"][0]
        self.assertEqual(agent["node_id"], NODE_A)
        self.assertEqual(agent["realized_as"], "vm")
        # Only the agent class reserves capacity; the host-service reference
        # reserves nothing (no VM is launched for it).
        self.assertEqual(commit.call_count, 1)
        args = commit.call_args[0]
        self.assertEqual(args[3], NODE_A)          # node_id
        self.assertEqual(args[4], 3)               # cpu_allocated: 3 x 1 vCPU
        self.assertEqual(args[5], 3)               # ram_allocated_gb: 3 x 1 GiB

    def test_no_front_door_match_is_rejected_with_a_useful_detail(self):
        plan, commit = self._plan([REAL_A, REAL_C], base_url="http://10.99.0.99:8080")
        self.assertEqual(plan["outcome"], "rejected")
        reasons = plan.get("reject_reasons", [])
        self.assertTrue(reasons)
        self.assertEqual(reasons[0]["reason"], "invalid_target_node_type")
        detail = reasons[0]["detail"]
        self.assertIn("inference node", detail)
        self.assertIn("agent-facing inference service", detail)
        self.assertEqual(commit.call_count, 0)

    def test_explicit_wrong_target_is_rejected_with_the_node_named(self):
        raw = [
            {"type": "agent_vm", "count": 1, "vcpus": 1, "ram_gb": 1, "disk_gb": 4},
            {"type": "inference_vm", "count": 1, "vcpus": 4, "ram_gb": 8, "disk_gb": 32,
             "target_node": NODE_A},
        ]
        plan, _ = self._plan([REAL_A, REAL_C], raw=raw)
        self.assertEqual(plan["outcome"], "rejected")
        reasons = plan.get("reject_reasons", [])
        self.assertEqual(reasons[0]["reason"], "invalid_target_node_type")
        self.assertIn(NODE_A, reasons[0]["detail"])
        self.assertIn("neither an inference node", reasons[0]["detail"])


if __name__ == "__main__":
    unittest.main()
