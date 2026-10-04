"""Offline tests for the inference on-prem allow-list (config §5.5).

Covers the frozen default (10.10.0.0/24 only) and the explicit, still-closed
widening used by the nested-LXD lab (`demo-node-c` on 10.14.100.0/24).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from termini_agent.config import ConfigError, load_config, validate_inference_url

BASE = """
project_id: 01J00000000000000000000000
vm_id: 01J00000000000000000000001
node_id: 01J00000000000000000000002
agent_image_version: agent-vm-2026.10.0
inference:
  base_url: {base_url}
{extra}  default_model: deepseek-flash
  allowed_models: [deepseek-flash, deepseek-v4-pro]
broker:
  endpoint: http://10.20.1.1:7071
logging:
  sink: /tmp/agent-test.jsonl
"""


def _write(text: str) -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "agent.yaml"
    p.write_text(text, encoding="utf-8")
    return p


class TestInferenceAllowList(unittest.TestCase):
    def test_default_rejects_lab_node_c(self) -> None:
        cfg = BASE.format(base_url="http://10.14.100.11:8080", extra="")
        with self.assertRaises(ConfigError):
            load_config(_write(cfg), dev_override=False)

    def test_explicit_lab_network_accepts_lab_node_c(self) -> None:
        cfg = BASE.format(
            base_url="http://10.14.100.11:8080",
            extra="  allowed_networks: [10.10.0.0/24, 10.14.100.0/24]\n",
        )
        loaded = load_config(_write(cfg), dev_override=False)
        self.assertEqual(loaded.inference.base_url, "http://10.14.100.11:8080")
        self.assertIn("10.14.100.0/24", loaded.inference.allowed_networks)

    def test_public_network_refused(self) -> None:
        cfg = BASE.format(base_url="http://10.10.0.13:8080",
                          extra="  allowed_networks: [0.0.0.0/0]\n")
        with self.assertRaises(ConfigError):
            load_config(_write(cfg), dev_override=False)

    def test_non_private_network_refused(self) -> None:
        cfg = BASE.format(base_url="http://10.10.0.13:8080",
                          extra="  allowed_networks: [10.10.0.0/24, 8.8.8.0/24]\n")
        with self.assertRaises(ConfigError):
            load_config(_write(cfg), dev_override=False)

    def test_validate_url_honours_explicit_networks(self) -> None:
        validate_inference_url("http://10.14.100.11:8080",
                               allowed_networks=("10.14.100.0/24",))
        with self.assertRaises(ConfigError):
            validate_inference_url("http://10.14.100.11:8080",
                                   allowed_networks=("10.10.0.0/24",))

    def test_default_network_still_frozen(self) -> None:
        # The contract value must keep working with no config change at all.
        validate_inference_url("http://10.10.0.13:8080")


if __name__ == "__main__":
    unittest.main(verbosity=2)
