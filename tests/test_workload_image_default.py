"""DEFECT 3 regression — the controller's workload-image default must match the
alias the node bake actually publishes.

MEASURED DEFECT (live standing fleet, 2026-10-13). The controller's default alias
was the bare ``agent-vm`` (``controller/app/config.py`` -> ``or "agent-vm"``), but
the node bake stores the WORKLOAD image as ``agent-vm/<semver>``
(``image/agent-vm/build-agent-image.sh``: ``ALIAS_BASE/version``;
``docs/contracts/agent-runtime.md`` §3.1/§4.2). So an out-of-the-box
``POST /vms/apply`` failed on a freshly-baked node with
``create ... failed: Image not provided for instance creation`` — the alias did not
exist in the node's local LXD store — and it had to be set by
``TERMINI_LXD_WORKLOAD_IMAGE=agent-vm/2026.10.0``. The code default and the bake
must agree.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))

from app import config  # noqa: E402

MANIFEST = REPO / "image" / "agent-vm" / "evidence" / "image-manifest.json"


class WorkloadImageDefaultTest(unittest.TestCase):
    def test_default_equals_the_baked_image_alias(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(
            config.DEFAULT_LXD_WORKLOAD_IMAGE, manifest["alias"],
            "controller default alias drifted from the baked workload image")

    def test_default_is_a_versioned_contract_alias(self):
        # agent-runtime.md §3.1: the alias is `agent-vm/<semver>`. A bare
        # `agent-vm` matches nothing in a baked node's LXD store.
        self.assertRegex(config.DEFAULT_LXD_WORKLOAD_IMAGE,
                         r"^agent-vm/\d+\.\d+\.\d+$")

    def test_config_load_falls_back_to_the_baked_alias(self):
        with mock.patch.dict(os.environ, {"TERMINI_BOOTSTRAP_TOKEN": "b1"}, clear=False):
            os.environ.pop("TERMINI_LXD_WORKLOAD_IMAGE", None)
            cfg = config.Config.load()
        self.assertEqual(cfg.lxd_workload_image, config.DEFAULT_LXD_WORKLOAD_IMAGE)
        self.assertNotEqual(cfg.lxd_workload_image, "agent-vm")

    def test_env_still_overrides_the_default(self):
        env = {"TERMINI_BOOTSTRAP_TOKEN": "b1",
               "TERMINI_LXD_WORKLOAD_IMAGE": "agent-vm/2099.1.1"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = config.Config.load()
        self.assertEqual(cfg.lxd_workload_image, "agent-vm/2099.1.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
