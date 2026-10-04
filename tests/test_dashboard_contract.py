"""Contract conformance tests for the dashboard mock fixtures.

These run in CI (``python3 -m unittest discover -s tests``) and pin the mock
fixtures to the frozen contracts the dashboard is built against:

  * docs/contracts/dashboard.md  §1 telemetry, §2 log envelope, §3 SSH string, §4 intervention
  * docs/telemetry-schema.md     §3 Sample object
  * controller/app/store.py      METRICS registry (metric-name drift guard)

If the contract moves, this test fails loudly instead of letting the dashboard
quietly diverge. The file loads ``dashboard/mock/fixtures.py`` by path (the
``dashboard`` directory is not a Python package).
"""
from __future__ import annotations

import importlib.util
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES_PATH = os.path.join(REPO_ROOT, "dashboard", "mock", "fixtures.py")
STORE_PATH = os.path.join(REPO_ROOT, "controller", "app", "store.py")

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z$")


def load_fixtures():
    spec = importlib.util.spec_from_file_location("termini_dashboard_fixtures", FIXTURES_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fx = load_fixtures()


class MetricRegistryTests(unittest.TestCase):
    """dashboard.md §1.4 freezes 19 metric names + units."""

    EXPECTED = {
        "cpu_util_pct": "percent",
        "cpu_core_count": "count",
        "ram_total_bytes": "bytes",
        "ram_used_bytes": "bytes",
        "swap_used_bytes": "bytes",
        "load_avg_1m": "loadavg",
        "cpu_temp_celsius": "celsius",
        "gpu_util_pct": "percent",
        "gpu_vram_used_bytes": "bytes",
        "gpu_vram_total_bytes": "bytes",
        "net_rx_bytes": "bytes",
        "net_tx_bytes": "bytes",
        "net_latency_ms": "ms",
        "disk_total_bytes": "bytes",
        "disk_free_bytes": "bytes",
        "disk_write_latency_ms": "ms",
        "inference_tokens_per_s": "tok/s",
        "inference_active_requests": "count",
        "inference_vram_used_bytes": "bytes",
    }

    def test_registry_matches_frozen_allowlist(self):
        self.assertEqual(fx.METRIC_UNITS, self.EXPECTED)
        self.assertEqual(len(fx.METRIC_UNITS), 19)

    def test_registry_matches_controller_store(self):
        """Drift guard against controller/app/store.py METRICS."""
        with open(STORE_PATH, encoding="utf-8") as fh:
            src = fh.read()
        block = src.split("METRICS: dict", 1)[1].split("\n}", 1)[0]
        names = re.findall(r'"([a-z0-9_]+)":\s*\(', block)
        self.assertEqual(sorted(names), sorted(self.EXPECTED), "controller METRICS drifted from the dashboard contract")

    def test_every_metric_resolves_on_an_inference_node(self):
        node = next(n for n in fx.NODES if n["role"] == "inference")
        s = fx.sample(node, 1000.0, 1)
        for name in fx.METRIC_UNITS:
            v = fx.metric_value(name, s)
            self.assertIsInstance(v, (int, float), f"{name} -> {v!r}")
            self.assertNotIsInstance(v, bool)


class NodeViewTests(unittest.TestCase):
    """dashboard.md §1.1/§1.3."""

    def test_view_shape_and_enums(self):
        node = fx.NODES[0]
        s = fx.sample(node, 1000.0, 1)
        view = fx.node_view(node, s, 1_000_000, 999_000)
        self.assertRegex(view["node_id"], ULID_RE)
        self.assertIn(view["role"], ("compute", "inference"))
        self.assertIn(view["status"], ("online", "degraded", "offline"))
        self.assertRegex(view["last_seen_at"], RFC3339_RE)
        for key in ("node_id", "registration_id", "role", "status", "last_seen_at", "hardware", "capacity", "health"):
            self.assertIn(key, view)
        self.assertIsInstance(view["capacity"]["cpu_cores"], int)
        self.assertIsInstance(view["hardware"]["memory"]["total_bytes"], int)

    def test_health_keys_and_reason_enum(self):
        node = fx.NODES[2]
        s = fx.sample(node, 1000.0, 1)
        h = fx.derive_health(node, s)
        for key in ("cpu_util_pct", "ram_used_pct", "disk_ok", "gpu_ok", "inference_ok", "services", "degraded_reasons"):
            self.assertIn(key, h)
        allowed = {
            "heartbeat_late", "disk_read_only", "disk_degrading", "gpu_unavailable",
            "gpu_overtemp", "inference_down", "service_failed",
        }
        self.assertTrue(set(h["degraded_reasons"]) <= allowed)

    def test_status_derivation_thresholds(self):
        node = fx.NODES[1]
        s = fx.sample(node, 1000.0, 1)
        now = 1_000_000
        self.assertEqual(fx.derive_status(node, s, now, now - 1_000)[0], "online")
        self.assertEqual(fx.derive_status(node, s, now, now - 20_000)[0], "degraded")
        self.assertEqual(fx.derive_status(node, s, now, now - 60_000)[0], "offline")

    def test_read_only_disk_and_gpu_overtemp_degrade(self):
        compute = fx.NODES[0]
        s = fx.sample(compute, 1000.0, 1)
        s["disk"][0]["mount_ro"] = True
        h = fx.derive_health(compute, s)
        self.assertFalse(h["disk_ok"])
        self.assertIn("disk_read_only", h["degraded_reasons"])

        inf = fx.NODES[2]
        s2 = fx.sample(inf, 1000.0, 1)
        s2["gpu"][0]["temp_celsius"] = 91
        h2 = fx.derive_health(inf, s2)
        self.assertFalse(h2["gpu_ok"])
        self.assertIn("gpu_overtemp", h2["degraded_reasons"])


class SampleTests(unittest.TestCase):
    """telemetry-schema.md §3."""

    def test_sample_shape_and_c1_transforms(self):
        s = fx.sample(fx.NODES[2], 1000.0, 7)
        self.assertEqual(s["seq"], 7)
        self.assertRegex(s["ts"], RFC3339_RE)
        for key in ("cpu_util_pct", "ram_total_bytes", "ram_used_bytes", "disk", "net", "gpu", "services", "inference"):
            self.assertIn(key, s)
        self.assertIsInstance(s["disk"], list)
        self.assertIsInstance(s["net"], list)
        self.assertTrue(s["gpu"], "inference node must report a GPU")
        # C1 fields must arrive already transformed (hash / /24 / 16-hex)
        self.assertRegex(s["hostname"], r"^[0-9a-f]{16}$")
        self.assertRegex(s["mac_address"], r"^[0-9a-f]{16}$")
        self.assertTrue(s["ip_local"].endswith("/24"))
        self.assertNotIn("ip_public", s)

    def test_service_allowlist(self):
        for node in fx.NODES:
            s = fx.sample(node, 1000.0, 1)
            for svc in s["services"]:
                self.assertIn(svc["name"], fx.SERVICE_ALLOWLIST)


class LogEnvelopeTests(unittest.TestCase):
    """dashboard.md §2.1."""

    def test_envelope(self):
        line = fx.log_line("01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C1E", "warn", "ai_operator", "msg",
                           {"command_id": fx.ulid(), "type": "node.drain", "status": "pending_approval"})
        self.assertRegex(line["log_id"], ULID_RE)
        self.assertRegex(line["ts"], RFC3339_RE)
        self.assertIn(line["severity"], ("debug", "info", "warn", "error"))
        self.assertIn(line["actor"], ("ai_operator", "human"))
        self.assertLessEqual(len(line["message"]), 1000)
        self.assertIn("remediation", line)
        self.assertIn(line["remediation"]["status"],
                      ("queued", "pending_approval", "approved", "denied", "acked", "running", "succeeded", "failed", "timed_out"))

    def test_project_level_line_has_null_node(self):
        line = fx.log_line(None, "info", "ai_operator", "project level")
        self.assertIsNone(line["node_id"])
        self.assertIsNone(line["remediation"])


class SshStringTests(unittest.TestCase):
    """dashboard.md §3.2."""

    def test_command_format(self):
        view = fx.ssh_view(fx.NODES[0])
        for key in ("node_id", "user", "host", "port", "key_algorithm", "key_fingerprint", "key_comment", "command"):
            self.assertIn(key, view)
        self.assertEqual(view["command"], f"ssh -i ~/.ssh/termini_breakglass {view['user']}@{view['host']}")
        self.assertNotIn("PRIVATE KEY", view["command"])


class IntentPlanTests(unittest.TestCase):
    """controller-api.md §4.2."""

    def test_accepted_plan_shape(self):
        plan = fx.intent_plan("deploy 3 workers")
        self.assertEqual(plan["outcome"], "accepted")
        self.assertRegex(plan["plan_id"], ULID_RE)
        for alloc in plan["allocations"]:
            for key in ("index", "status", "requested", "resolved", "guardrail", "node_id"):
                self.assertIn(key, alloc)
            self.assertIn("clamped_fields", alloc["guardrail"])
            self.assertIn("reject_reasons", alloc["guardrail"])

    def test_rejected_plan_carries_reasons(self):
        plan = fx.intent_plan("an impossible request")
        self.assertEqual(plan["outcome"], "rejected")
        reasons = [r for a in plan["allocations"] for r in a["guardrail"]["reject_reasons"]]
        self.assertTrue(reasons)


class SeriesTests(unittest.TestCase):
    """dashboard.md §1.4."""

    def test_series_points_and_units(self):
        out = fx.series(fx.NODES[2], 1000.0, 1030.0, "5s", ["cpu_util_pct", "gpu_util_pct", "inference_tokens_per_s"])
        self.assertEqual(out["resolution"], "5s")
        self.assertEqual({s["name"] for s in out["series"]}, {"cpu_util_pct", "gpu_util_pct", "inference_tokens_per_s"})
        for s in out["series"]:
            self.assertEqual(s["unit"], fx.METRIC_UNITS[s["name"]])
            self.assertGreater(len(s["points"]), 0)
            for p in s["points"]:
                self.assertRegex(p["t"], RFC3339_RE)
                self.assertIsInstance(p["v"], (int, float))

    def test_series_buckets_by_resolution(self):
        raw = fx.series(fx.NODES[0], 1000.0, 1300.0, "5s", ["cpu_util_pct"])
        coarse = fx.series(fx.NODES[0], 1000.0, 1300.0, "1m", ["cpu_util_pct"])
        self.assertLess(len(coarse["series"][0]["points"]), len(raw["series"][0]["points"]))


class UlidTests(unittest.TestCase):
    def test_ulid_is_26_char_crockford_and_increases(self):
        a = fx.ulid(1_700_000_000_000)
        b = fx.ulid(1_700_000_000_000)
        self.assertRegex(a, ULID_RE)
        self.assertRegex(b, ULID_RE)
        self.assertNotEqual(a, b)
        self.assertLess(a, b, "ULIDs must be lexicographically increasing (log cursor)")


if __name__ == "__main__":
    unittest.main()
