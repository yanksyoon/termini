"""Launch-time agent config (agent-runtime.md §4.4) — controller/app/agent_provision.py.

The defect these pin: `_apply_plan` created instances with NO cloud-init
user-data, so an instance produced by `Scale intent → Apply` booted with the
bake's placeholder ids and a termini-agent that was never enabled (the bake
leaves it disabled on purpose — §4.4 makes the launch responsible). A guest
whose agent is not running cannot answer the task surface, so "Give task" could
never succeed for an Apply-created instance.

Run: python3 -m unittest tests.test_agent_provision
"""
import os
import re
import unittest
from pathlib import Path

import yaml

from controller.app import agent_provision
from controller.app.operator import vmids

REPO = Path(__file__).resolve().parents[1]
BAKED_TEMPLATE = REPO / "image" / "agent-vm" / "config" / "agent.yaml.template"


def _render(**over):
    kwargs = dict(
        project_id="01J0DEM0WARM000000000000AB",
        vm_id="NNB9287WRHGFDPEZ995TKCXDNW",
        node_id="01M4ZQ7Q2J80ATY3K32PY3PPJH",
        inference_base_url="http://10.10.0.13:8080/v1",
        allowed_models=["gemma"],
        default_model="gemma",
        image_version="agent-vm-2026.10.0",
    )
    kwargs.update(over)
    return agent_provision.render_user_data(**kwargs)


class RenderTest(unittest.TestCase):
    def test_user_data_is_a_cloud_config_that_writes_and_starts_the_agent(self):
        doc = yaml.safe_load(_render())
        self.assertIn("write_files", doc)
        files = {f["path"]: f for f in doc["write_files"]}
        self.assertIn("/etc/termini/agent.yaml", files)
        self.assertEqual(files["/etc/termini/agent.yaml"]["permissions"], "0600")
        # The service must be enabled AT LAUNCH: the bake leaves it disabled.
        self.assertEqual(doc["runcmd"], [["systemctl", "enable", "--now",
                                          "termini-agent.service"]])

    def test_the_written_config_carries_this_instance_ids(self):
        cfg = yaml.safe_load(yaml.safe_load(_render())["write_files"][0]["content"])
        self.assertEqual(cfg["project_id"], "01J0DEM0WARM000000000000AB")
        self.assertEqual(cfg["node_id"], "01M4ZQ7Q2J80ATY3K32PY3PPJH")
        self.assertEqual(cfg["vm_id"], "NNB9287WRHGFDPEZ995TKCXDNW")
        # The task surface bound and the inference the guest must dial.
        self.assertGreaterEqual(int(cfg["tasks"]["max_concurrent"]), 1)
        self.assertEqual(cfg["inference"]["base_url"], "http://10.10.0.13:8080/v1")
        self.assertIn("gemma", cfg["inference"]["allowed_models"])

    def test_the_rendered_keys_match_the_baked_template(self):
        """Schema parity with what the image was baked from (§4.4 is the same doc)."""
        baked = yaml.safe_load(BAKED_TEMPLATE.read_text(encoding="utf-8"))
        rendered = yaml.safe_load(
            yaml.safe_load(_render())["write_files"][0]["content"])
        self.assertEqual(set(rendered), set(baked))

        def walk(a, b, path=""):
            for key in b:
                here = f"{path}.{key}" if path else key
                self.assertIn(key, a, f"{here} missing from the rendered config")
                if isinstance(b[key], dict):
                    walk(a[key], b[key], here)

        walk(rendered, baked)

    def test_the_vm_id_is_the_id_the_node_will_report(self):
        """Telemetry reports `vm_id_for_instance(name)`; the guest must agree."""
        name = "termini-01m4zq7q2j80aty3k32py3ppjh-0-0"
        cfg = yaml.safe_load(yaml.safe_load(
            _render(vm_id=vmids.vm_id_for_instance(name)))["write_files"][0]["content"])
        self.assertEqual(cfg["vm_id"], vmids.vm_id_for_instance(name))


class InjectionTest(unittest.TestCase):
    def test_a_newline_in_an_id_is_refused_not_rendered(self):
        with self.assertRaises(agent_provision.AgentConfigError):
            _render(project_id="01J0DEM0\nruncmd:\n  - [ rm, -rf, / ]")

    def test_a_yaml_significant_character_is_refused(self):
        for bad in ("vm#1", "a: b", "[x]", "a{b}", "a|b"):
            with self.subTest(bad=bad), self.assertRaises(agent_provision.AgentConfigError):
                _render(node_id=bad)

    def test_an_empty_value_is_refused(self):
        with self.assertRaises(agent_provision.AgentConfigError):
            _render(vm_id="")


class InferenceAddressTest(unittest.TestCase):
    def test_slash_v1_is_appended_once(self):
        class Cfg:
            inference_base_url = "http://10.10.0.13:8080"

        self.assertEqual(agent_provision.agent_inference_base_url(Cfg()),
                         "http://10.10.0.13:8080/v1")

        class Cfg2:
            inference_base_url = "http://10.10.0.13:8080/v1/"

        self.assertEqual(agent_provision.agent_inference_base_url(Cfg2()),
                         "http://10.10.0.13:8080/v1")

    def test_the_guest_facing_knob_wins_over_the_operator_address(self):
        class Cfg:
            inference_base_url = "http://127.0.0.1:8085"

        old = os.environ.get("TERMINI_AGENT_INFERENCE_BASE_URL")
        os.environ["TERMINI_AGENT_INFERENCE_BASE_URL"] = "http://10.10.0.13:8080"
        try:
            self.assertEqual(agent_provision.agent_inference_base_url(Cfg()),
                             "http://10.10.0.13:8080/v1")
        finally:
            if old is None:
                del os.environ["TERMINI_AGENT_INFERENCE_BASE_URL"]
            else:
                os.environ["TERMINI_AGENT_INFERENCE_BASE_URL"] = old


if __name__ == "__main__":
    unittest.main()
