"""Unit tests for the intent extractor and the extract_and_validate pipeline.

The model client is always faked — no network. These tests assert that the
canonical prompt yields clamped specs under a constrained host, and that
malformed model output degrades to a rejected (safe, empty) result rather than
ever reaching LXD.
"""

from __future__ import annotations

import json
import unittest

from termini.intent import prompt as prompt_module
from termini.intent.extractor import (
    ExtractionError,
    _parse_allocations,
    extract,
    extract_and_validate,
)

CONSTRAINED_HOST = {
    "agent_vm": {"vcpus": 2, "ram_gb": 4, "disk_gb": 16},
    "inference_vm": {"vcpus": 4, "ram_gb": 8, "disk_gb": 32},
}

# Canonical extraction, but with deliberately over-sized numbers so the
# downstream clamp has real work to do (mirrors the adversarial "1000 vcpus"
# case from project.md note #5).
CANONICAL_RAW = {
    "allocations": [
        {"type": "agent_vm", "count": 3, "vcpus": 1000, "ram_gb": 512, "disk_gb": 8,
         "passthrough_gpu": False, "target_node": "auto"},
        {"type": "inference_vm", "count": 1, "vcpus": 128, "ram_gb": 512, "disk_gb": 64,
         "passthrough_gpu": True, "target_node": "auto"},
    ]
}


def fake_client(content):
    def _client(messages, **kwargs):
        return content
    return _client


class CanonicalPromptTest(unittest.TestCase):
    def test_canonical_prompt_clamped_under_constrained_host(self):
        result = extract_and_validate(
            "deploy 3 agents + 1 fast LLM",
            CONSTRAINED_HOST,
            client=fake_client(json.dumps(CANONICAL_RAW)),
        )
        self.assertTrue(result.safe, result.to_dict())
        self.assertEqual(len(result.allocations), 2)

        agent, inference = result.allocations
        self.assertEqual(agent.type, "agent_vm")
        self.assertEqual(agent.count, 3)
        self.assertEqual(inference.type, "inference_vm")
        self.assertEqual(inference.count, 1)
        self.assertTrue(inference.passthrough_gpu)

        for spec in result.allocations:
            role = spec.type
            host = CONSTRAINED_HOST[role]
            self.assertLessEqual(spec.vcpus, host["vcpus"])
            self.assertLessEqual(spec.ram_gb, host["ram_gb"])
            self.assertLessEqual(spec.disk_gb, host["disk_gb"])

    def test_every_numeric_field_within_host_max(self):
        result = extract_and_validate(
            "deploy 3 agents + 1 fast LLM",
            CONSTRAINED_HOST,
            client=fake_client(json.dumps(CANONICAL_RAW)),
        )
        for spec in result.allocations:
            host = CONSTRAINED_HOST[spec.type]
            for field, cap in (
                ("vcpus", host["vcpus"]),
                ("ram_gb", host["ram_gb"]),
                ("disk_gb", host["disk_gb"]),
            ):
                self.assertLessEqual(getattr(spec, field), cap, f"{spec.type}.{field} > {cap}")

    def test_clamp_decisions_reported(self):
        result = extract_and_validate(
            "deploy 3 agents + 1 fast LLM",
            CONSTRAINED_HOST,
            client=fake_client(json.dumps(CANONICAL_RAW)),
        )
        self.assertGreater(len(result.decisions), 0)
        self.assertEqual(result.outcome, "clamped")


class MalformedModelOutputTest(unittest.TestCase):
    def test_invalid_json_yields_rejected(self):
        result = extract_and_validate(
            "deploy 3 agents + 1 fast LLM",
            CONSTRAINED_HOST,
            client=fake_client("this is not json {{{"),
        )
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(result.allocations, [])
        self.assertEqual(result.rejections[0].reason, "extraction_failed")

    def test_missing_allocations_key_yields_rejected(self):
        result = extract_and_validate(
            "deploy 3 agents + 1 fast LLM",
            CONSTRAINED_HOST,
            client=fake_client('{"some_other_key": 1}'),
        )
        self.assertEqual(result.outcome, "rejected")

    def test_non_list_allocations_yields_rejected(self):
        result = extract_and_validate(
            "deploy 3 agents + 1 fast LLM",
            CONSTRAINED_HOST,
            client=fake_client('{"allocations": "nope"}'),
        )
        self.assertEqual(result.outcome, "rejected")

    def test_unknown_component_from_model_yields_rejected(self):
        raw = {"allocations": [{"type": "magic_host", "count": 1}]}
        result = extract_and_validate(
            "give me a magic host", CONSTRAINED_HOST, client=fake_client(json.dumps(raw))
        )
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(result.rejections[0].reason, "unknown_component_type")

    def test_client_raises_yields_rejected_not_exception(self):
        def boom(messages, **kwargs):
            raise ExtractionError("model down")

        result = extract_and_validate("deploy 3 agents", CONSTRAINED_HOST, client=boom)
        self.assertEqual(result.outcome, "rejected")
        self.assertFalse(result.allocations)


class ParseTest(unittest.TestCase):
    def test_parse_bare_list(self):
        self.assertEqual(_parse_allocations('[{"type": "agent_vm"}]'), [{"type": "agent_vm"}])

    def test_parse_envelope(self):
        self.assertEqual(
            _parse_allocations('{"allocations": [{"type": "agent_vm"}]}'),
            [{"type": "agent_vm"}],
        )

    def test_parse_strips_markdown_fences(self):
        self.assertEqual(
            _parse_allocations('```json\n[{"type": "agent_vm"}]\n```'),
            [{"type": "agent_vm"}],
        )

    def test_parse_non_object_entry_raises(self):
        with self.assertRaises(ExtractionError):
            _parse_allocations('["agent_vm"]')


class PromptTest(unittest.TestCase):
    def test_few_shot_covers_canonical_prompt(self):
        messages = prompt_module.build_messages("deploy 3 agents + 1 fast LLM")
        users = [m["content"] for m in messages if m["role"] == "user"]
        self.assertIn("deploy 3 agents + 1 fast LLM", users)  # few-shot example present
        self.assertIn("deploy 3 agents + 1 fast LLM", messages[-1]["content"])  # final prompt

    def test_extract_returns_raw_list(self):
        raw = extract(
            "deploy 3 agents", client=fake_client(json.dumps(CANONICAL_RAW))
        )
        self.assertEqual(raw, CANONICAL_RAW["allocations"])


if __name__ == "__main__":
    unittest.main()
