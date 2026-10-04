"""Table-driven unit tests for the deterministic clamp validator.

These tests assert the security boundary: every numeric field is floored,
integer-coerced, and clamped to ``min(requested, host_max)``; out-of-range,
malformed, unknown, and over-long input is rejected all-or-nothing; and the
validator is provably LLM-free (pure, no network imports).
"""

from __future__ import annotations

import inspect
import unittest

from termini.intent import validator as validator_module
from termini.intent.types import (
    ABSOLUTE_CAPS,
    DEFAULT_HOST_CAPS,
    FLOORS,
    MAX_COMPONENTS,
    HostCaps,
    ValidationResult,
)
from termini.intent.validator import clamp_validate

HOST_CAPS = {
    "agent_vm": {"vcpus": 4, "ram_gb": 8, "disk_gb": 32},
    "inference_vm": {"vcpus": 8, "ram_gb": 16, "disk_gb": 64},
}


class ClampMathTest(unittest.TestCase):
    """min(requested, host_max) applied on every numeric field."""

    def _clamp_one(self, field, requested):
        component = {"type": "agent_vm", "count": 1, "vcpus": 2, "ram_gb": 4, "disk_gb": 8}
        component[field] = requested
        result = clamp_validate([component], HOST_CAPS)
        self.assertTrue(result.safe, result.to_dict())
        spec = result.allocations[0]
        return getattr(spec, field)

    def test_vcpus_clamped_to_host_max(self):
        self.assertEqual(self._clamp_one("vcpus", 100), HOST_CAPS["agent_vm"]["vcpus"])

    def test_ram_clamped_to_host_max(self):
        self.assertEqual(self._clamp_one("ram_gb", 512), HOST_CAPS["agent_vm"]["ram_gb"])

    def test_disk_clamped_to_host_max(self):
        self.assertEqual(self._clamp_one("disk_gb", 1000), HOST_CAPS["agent_vm"]["disk_gb"])

    def test_in_range_value_unchanged(self):
        self.assertEqual(self._clamp_one("vcpus", 2), 2)
        self.assertEqual(self._clamp_one("ram_gb", 4), 4)

    def test_exact_boundary_unchanged(self):
        self.assertEqual(self._clamp_one("vcpus", HOST_CAPS["agent_vm"]["vcpus"]), 4)

    def test_clamp_decision_reported(self):
        result = clamp_validate(
            [{"type": "agent_vm", "count": 1, "vcpus": 100, "ram_gb": 512, "disk_gb": 8}],
            HOST_CAPS,
        )
        reasons = {d.field: (d.requested, d.applied, d.reason) for d in result.decisions}
        self.assertEqual(reasons["vcpus"], (100, 4, "above_host_max"))
        self.assertEqual(reasons["ram_gb"], (512, 8, "above_host_max"))
        self.assertEqual(result.outcome, "clamped")


class BoundsAndCoercionTest(unittest.TestCase):
    def test_below_minimum_floored(self):
        result = clamp_validate(
            [{"type": "agent_vm", "count": 1, "vcpus": -5, "ram_gb": 0, "disk_gb": 0}],
            HOST_CAPS,
        )
        self.assertTrue(result.safe)
        spec = result.allocations[0]
        self.assertEqual(spec.vcpus, FLOORS["vcpus"])
        self.assertEqual(spec.ram_gb, FLOORS["ram_gb"])
        self.assertEqual(spec.disk_gb, FLOORS["disk_gb"])
        self.assertIn("below_minimum", {d.reason for d in result.decisions})

    def test_float_truncated_to_int(self):
        result = clamp_validate(
            [{"type": "agent_vm", "count": 1, "vcpus": 3.9, "ram_gb": 4.5, "disk_gb": 8.0}],
            HOST_CAPS,
        )
        spec = result.allocations[0]
        self.assertEqual(spec.vcpus, 3)
        self.assertEqual(spec.ram_gb, 4)

    def test_numeric_string_coerced(self):
        result = clamp_validate(
            [{"type": "agent_vm", "count": 1, "vcpus": "2", "ram_gb": "4", "disk_gb": "8"}],
            HOST_CAPS,
        )
        self.assertTrue(result.safe)
        self.assertEqual(result.allocations[0].vcpus, 2)

    def test_bool_not_accepted_as_int(self):
        result = clamp_validate(
            [{"type": "agent_vm", "count": True, "vcpus": 2, "ram_gb": 4, "disk_gb": 8}],
            HOST_CAPS,
        )
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(result.rejections[0].reason, "non_integer")

    def test_non_numeric_rejected(self):
        result = clamp_validate(
            [{"type": "agent_vm", "count": "lots", "vcpus": 2, "ram_gb": 4, "disk_gb": 8}],
            HOST_CAPS,
        )
        self.assertEqual(result.outcome, "rejected")
        self.assertFalse(result.allocations)

    def test_absolute_cap_still_applies_over_host_max(self):
        # count has no host cap; it must respect the absolute structural cap.
        result = clamp_validate(
            [{"type": "agent_vm", "count": 9999, "vcpus": 2, "ram_gb": 4, "disk_gb": 8}],
            HOST_CAPS,
        )
        self.assertTrue(result.safe)
        self.assertEqual(result.allocations[0].count, ABSOLUTE_CAPS["count"])


class RejectionTest(unittest.TestCase):
    def test_unknown_component_type_rejected(self):
        result = clamp_validate(
            [{"type": "gpu_host", "count": 1, "vcpus": 2, "ram_gb": 4, "disk_gb": 8}],
            HOST_CAPS,
        )
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(result.rejections[0].reason, "unknown_component_type")
        self.assertFalse(result.allocations)  # all-or-nothing

    def test_one_bad_component_poisons_all(self):
        result = clamp_validate(
            [
                {"type": "agent_vm", "count": 1, "vcpus": 2, "ram_gb": 4, "disk_gb": 8},
                {"type": "inference_vm", "count": 1, "vcpus": 8, "ram_gb": 16, "disk_gb": 64},
                {"type": "mystery_host", "count": 1},
            ],
            HOST_CAPS,
        )
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(result.allocations, [])
        self.assertEqual(len(result.rejections), 1)

    def test_missing_type_rejected(self):
        result = clamp_validate([{"count": 1, "vcpus": 2}], HOST_CAPS)
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(result.rejections[0].reason, "missing_component_type")

    def test_missing_count_rejected(self):
        result = clamp_validate([{"type": "agent_vm", "vcpus": 2}], HOST_CAPS)
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(result.rejections[0].reason, "missing_required_field")

    def test_non_dict_component_rejected(self):
        result = clamp_validate(["not a dict"], HOST_CAPS)
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(result.rejections[0].reason, "malformed_component")

    def test_non_list_input_rejected(self):
        result = clamp_validate({"allocations": "nope"}, HOST_CAPS)
        self.assertEqual(result.outcome, "rejected")

    def test_empty_components_rejected(self):
        result = clamp_validate([], HOST_CAPS)
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(result.rejections[0].reason, "empty_components")

    def test_max_component_count_rejected(self):
        components = [
            {"type": "agent_vm", "count": 1, "vcpus": 1, "ram_gb": 1, "disk_gb": 4}
            for _ in range(MAX_COMPONENTS + 1)
        ]
        result = clamp_validate(components, HOST_CAPS)
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(result.rejections[0].reason, "max_component_count_exceeded")

    def test_invalid_host_caps_rejected(self):
        result = clamp_validate(
            [{"type": "agent_vm", "count": 1, "vcpus": 2, "ram_gb": 4, "disk_gb": 8}],
            {"agent_vm": {"vcpus": 0, "ram_gb": 16, "disk_gb": 64}, "inference_vm": {}},
        )
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(result.rejections[0].reason, "malformed_component")

    def test_passthrough_gpu_true_rejected(self):
        # controller-api.md §5.3: `passthrough_gpu: true` on any class is a
        # structural rejection (reason gpu_passthrough_not_in_demo), never clamped.
        result = clamp_validate(
            [{"type": "agent_vm", "count": 1, "vcpus": 2, "ram_gb": 4, "disk_gb": 8,
              "passthrough_gpu": True}],
            HOST_CAPS,
        )
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(result.allocations, [])
        self.assertEqual(result.rejections[0].reason, "gpu_passthrough_not_in_demo")

    def test_passthrough_gpu_true_rejected_on_inference_vm(self):
        result = clamp_validate(
            [{"type": "inference_vm", "count": 1, "vcpus": 8, "ram_gb": 16, "disk_gb": 64,
              "passthrough_gpu": True}],
            HOST_CAPS,
        )
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(result.allocations, [])
        self.assertEqual(result.rejections[0].reason, "gpu_passthrough_not_in_demo")

    def test_passthrough_gpu_true_poisons_otherwise_valid_components(self):
        # All-or-nothing: one passthrough class rejects the whole request.
        result = clamp_validate(
            [
                {"type": "agent_vm", "count": 1, "vcpus": 2, "ram_gb": 4, "disk_gb": 8},
                {"type": "inference_vm", "count": 1, "vcpus": 8, "ram_gb": 16,
                 "disk_gb": 64, "passthrough_gpu": True},
            ],
            HOST_CAPS,
        )
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(result.allocations, [])

    def test_passthrough_gpu_false_accepted(self):
        result = clamp_validate(
            [{"type": "agent_vm", "count": 1, "vcpus": 2, "ram_gb": 4, "disk_gb": 8,
              "passthrough_gpu": False}],
            HOST_CAPS,
        )
        self.assertTrue(result.safe)
        self.assertFalse(result.allocations[0].passthrough_gpu)


class EnvelopeTest(unittest.TestCase):
    def test_allocations_envelope(self):
        result = clamp_validate(
            {"allocations": [{"type": "agent_vm", "count": 3, "vcpus": 2, "ram_gb": 4, "disk_gb": 8}]},
            HOST_CAPS,
        )
        self.assertTrue(result.safe)
        self.assertEqual(result.allocations[0].count, 3)

    def test_components_envelope(self):
        result = clamp_validate(
            {"components": [{"type": "agent_vm", "count": 1, "vcpus": 2, "ram_gb": 4, "disk_gb": 8}]},
            HOST_CAPS,
        )
        self.assertTrue(result.safe)


class HostCapsTest(unittest.TestCase):
    def test_shared_caps_applied_to_all_roles(self):
        caps = HostCaps.from_dict({"vcpus": 4, "ram_gb": 8, "disk_gb": 16})
        self.assertEqual(caps.agent_vm.vcpus, 4)
        self.assertEqual(caps.inference_vm.vcpus, 4)

    def test_default_caps(self):
        caps = HostCaps.from_dict(None)
        self.assertEqual(caps.agent_vm.vcpus, DEFAULT_HOST_CAPS["agent_vm"]["vcpus"])

    def test_below_floor_raises(self):
        with self.assertRaises(ValueError):
            HostCaps.from_dict({"vcpus": 0, "ram_gb": 8, "disk_gb": 16})

    def test_above_absolute_cap_raises(self):
        with self.assertRaises(ValueError):
            HostCaps.from_dict({"vcpus": 8, "ram_gb": 16, "disk_gb": 99999})


class LLMFreeTest(unittest.TestCase):
    """The validator must be provably pure: no network, no model, no I/O."""

    def _imported_modules(self):
        import ast

        tree = ast.parse(inspect.getsource(validator_module))
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mods.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    mods.add(node.module.split(".")[0])
        return mods

    def test_no_network_or_model_imports(self):
        mods = self._imported_modules()
        forbidden = {
            "urllib",
            "socket",
            "http",
            "requests",
            "openai",
            "anthropic",
            "subprocess",
            "os",
        }
        self.assertFalse(
            mods & forbidden, f"validator imports forbidden modules: {mods & forbidden}"
        )

    def test_imports_only_types_and_stdlib(self):
        mods = self._imported_modules()
        self.assertLessEqual(mods, {"__future__", "math", "typing", "types"})
        # The only project import is the relative ``from .types import ...``;
        # assert it never pulls in the extractor (which imports urllib).
        self.assertNotIn("extractor", mods)


if __name__ == "__main__":
    unittest.main()
