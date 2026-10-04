"""Unit tests for the termini-agent runtime (run at bake time via 35-agent-selftest.sh).

Stdlib + baked deps only (unittest, no pytest) so they execute inside the image.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from termini_agent import __version__
from termini_agent.broker import Task
from termini_agent.config import ConfigError, load_config, validate_inference_url
from termini_agent.logging_setup import configure_logging
from termini_agent.ulid import new_ulid

GOOD = """
project_id: 01J00000000000000000000000
vm_id: 01J00000000000000000000001
node_id: 01J00000000000000000000002
agent_image_version: agent-vm-2026.10.0
inference:
  base_url: http://10.10.0.13:8080
  default_model: deepseek-flash
  allowed_models: [deepseek-flash, deepseek-v4-pro]
broker:
  endpoint: http://10.20.1.1:7071
logging:
  sink: /tmp/agent-test.jsonl
"""

BAD_HOST = GOOD.replace("http://10.10.0.13:8080", "https://api.openai.com/v1")
BAD_MODEL = GOOD.replace("default_model: deepseek-flash", "default_model: gpt-4")


def _write(text: str) -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "agent.yaml"
    p.write_text(text, encoding="utf-8")
    return p


class TestConfig(unittest.TestCase):
    def test_good_config_loads(self) -> None:
        cfg = load_config(_write(GOOD), dev_override=False)
        self.assertEqual(cfg.inference.base_url, "http://10.10.0.13:8080")
        self.assertEqual(cfg.inference.default_model, "deepseek-flash")
        self.assertIn("deepseek-v4-pro", cfg.inference.allowed_models)

    def test_off_prem_host_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(_write(BAD_HOST), dev_override=False)

    def test_default_model_must_be_allowed(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(_write(BAD_MODEL), dev_override=False)

    def test_validate_url_rejects_public(self) -> None:
        with self.assertRaises(ConfigError):
            validate_inference_url("http://8.8.8.8:8080")

    def test_validate_url_accepts_onprem(self) -> None:
        validate_inference_url("http://10.10.0.13:8080")

    def test_public_dict_has_no_token(self) -> None:
        cfg = load_config(_write(GOOD), dev_override=False)
        self.assertNotIn("token", json.dumps(cfg.as_public_dict()).lower())


class TestUlid(unittest.TestCase):
    def test_shape_and_charset(self) -> None:
        u = new_ulid()
        self.assertEqual(len(u), 26)
        self.assertTrue(all(c in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for c in u))

    def test_sorted_by_time(self) -> None:
        import time as _time

        first = new_ulid()
        _time.sleep(0.005)  # ensure the ms timestamp advances (ULID time prefix)
        second = new_ulid()
        self.assertLess(first[:10], second[:10])


class TestTask(unittest.TestCase):
    def test_from_json_defaults(self) -> None:
        t = Task.from_json({"task_id": "01J0", "kind": "chat"})
        self.assertEqual(t.task_id, "01J0")
        self.assertEqual(t.ttl_s, 300)
        self.assertIsNone(t.model_preference)


class TestInferenceRouting(unittest.TestCase):
    def test_resolve_model(self) -> None:
        from termini_agent.inference import InferenceClient, ModelNotAllowed

        cfg = load_config(_write(GOOD), dev_override=False)
        client = InferenceClient(cfg)
        self.assertEqual(client.resolve_model(None), "deepseek-flash")
        self.assertEqual(client.resolve_model("deepseek-v4-pro"), "deepseek-v4-pro")
        with self.assertRaises(ModelNotAllowed):
            client.resolve_model("gpt-4")

    def test_attribution_headers(self) -> None:
        from termini_agent.inference import InferenceClient

        cfg = load_config(_write(GOOD), dev_override=False)
        client = InferenceClient(cfg)
        h = client.headers("01JTESTREQUESTID0000000000")
        self.assertEqual(h["X-Termini-Request-Id"], "01JTESTREQUESTID0000000000")
        self.assertEqual(h["X-Termini-Vm-Id"], cfg.vm_id)


class TestLogging(unittest.TestCase):
    def test_jsonl_line(self) -> None:
        import logging

        sink = Path(tempfile.mkdtemp()) / "agent.jsonl"
        configure_logging(str(sink))
        logging.getLogger("termini_agent.test").info("hello", extra={"termini": {"task_id": "01J0"}})
        line = json.loads(sink.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(line["msg"], "hello")
        self.assertEqual(line["task_id"], "01J0")


class TestVersion(unittest.TestCase):
    def test_version(self) -> None:
        self.assertEqual(__version__, "2026.10.0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
