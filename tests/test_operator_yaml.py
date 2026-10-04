"""Tests for the stdlib-only YAML-subset parser (operator/yaml.py)."""
from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))

from app.operator import yaml  # noqa: E402


class ScalarTest(unittest.TestCase):
    def test_string(self):
        self.assertEqual(yaml.loads("a: hello"), {"a": "hello"})

    def test_int(self):
        self.assertEqual(yaml.loads("a: 42"), {"a": 42})

    def test_negative_int(self):
        self.assertEqual(yaml.loads("a: -5"), {"a": -5})

    def test_float(self):
        self.assertEqual(yaml.loads("a: 0.95"), {"a": 0.95})

    def test_negative_float(self):
        self.assertEqual(yaml.loads("a: -5000000.0"), {"a": -5000000.0})

    def test_bool(self):
        self.assertEqual(yaml.loads("a: true"), {"a": True})
        self.assertEqual(yaml.loads("a: false"), {"a": False})

    def test_null(self):
        self.assertEqual(yaml.loads("a: null"), {"a": None})

    def test_quoted(self):
        self.assertEqual(yaml.loads('a: "hello world"'), {"a": "hello world"})

    def test_comment_stripped(self):
        self.assertEqual(yaml.loads("a: 1  # trailing"), {"a": 1})

    def test_full_line_comment(self):
        self.assertEqual(yaml.loads("# hi\na: 1"), {"a": 1})


class MappingTest(unittest.TestCase):
    def test_nested(self):
        doc = "action:\n  kind: instance.restart\n  params:\n    force: true\n"
        self.assertEqual(yaml.loads(doc), {
            "action": {"kind": "instance.restart", "params": {"force": True}}})

    def test_three_levels(self):
        doc = "a:\n  b:\n    c: 3\n"
        self.assertEqual(yaml.loads(doc), {"a": {"b": {"c": 3}}})


class SequenceTest(unittest.TestCase):
    def test_sequence_of_scalars(self):
        self.assertEqual(yaml.loads("items:\n  - a\n  - b\n"), {"items": ["a", "b"]})

    def test_sequence_of_mappings(self):
        doc = "steps:\n  - kind: a\n    target: x\n  - kind: b\n    target: y\n"
        self.assertEqual(yaml.loads(doc), {
            "steps": [{"kind": "a", "target": "x"}, {"kind": "b", "target": "y"}]})

    def test_flow_sequence(self):
        self.assertEqual(yaml.loads("a: [1, 2, 3]"), {"a": [1, 2, 3]})

    def test_flow_mapping(self):
        self.assertEqual(yaml.loads("a: {x: 1, y: 2}"), {"a": {"x": 1, "y": 2}})


class PlaybookShapeTest(unittest.TestCase):
    def test_oom_playbook_parses(self):
        doc = pathlib.Path(REPO / "controller" / "playbooks" / "oom.yaml").read_text()
        parsed = yaml.loads(doc)
        self.assertEqual(parsed["type"], "oom")
        self.assertEqual(parsed["action"]["kind"], "instance.restart")
        self.assertEqual(parsed["action"]["target"], "heaviest")
        self.assertTrue(parsed["action"]["params"]["force"])

    def test_disk_playbook_parses(self):
        doc = pathlib.Path(REPO / "controller" / "playbooks" / "disk-degrade.yaml").read_text()
        parsed = yaml.loads(doc)
        self.assertEqual(parsed["type"], "disk_degrade")
        self.assertEqual(parsed["action"]["kind"], "instance.stop")
        self.assertEqual(parsed["action"]["target"], "heaviest")

    def test_latency_playbook_parses(self):
        doc = pathlib.Path(REPO / "controller" / "playbooks" / "latency.yaml").read_text()
        parsed = yaml.loads(doc)
        self.assertEqual(parsed["type"], "latency")
        self.assertEqual(parsed["action"]["kind"], "instance.restart")


if __name__ == "__main__":
    unittest.main(verbosity=2)
