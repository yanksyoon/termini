"""Unit tests for the chaos/fault harness (fast; no controller booted)."""
from __future__ import annotations

import pathlib
import sys
import threading
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from chaos import common, config, lxd_stub, node_sim, reference_operator  # noqa: E402

NODE = {
    "hostname": "node-chaos",
    "cpu_core_count": 16,
    "ram_total_bytes": 34359738368,
    "disk_total_bytes": 274877906944,
    "gpu": False,
}


def _series(cfg, mode, params, n):
    vm = common.new_ulid()
    return [
        node_sim.generate_sample(
            node=NODE, project_id=common.new_ulid(), vm_id=vm, mode=mode,
            params=params, seq=i + 1, elapsed_s=float(i), ts=common.rfc3339_now(),
        )
        for i in range(n)
    ]


class TestUlid(unittest.TestCase):
    def test_valid_and_unique(self):
        a, b = common.new_ulid(), common.new_ulid()
        self.assertTrue(common.is_ulid(a))
        self.assertTrue(common.is_ulid(b))
        self.assertNotEqual(a, b)
        self.assertFalse(common.is_ulid("has-illegal-characters"))  # contains I/O/L/U


class TestConfig(unittest.TestCase):
    def test_defaults_load(self):
        cfg = config.load()
        self.assertIn("oom", config.scenario_names(cfg))
        self.assertEqual(cfg["thresholds"]["oom_ram_pct"], 95.0)
        self.assertEqual(cfg["node"]["cadence_ms"], 1000)

    def test_env_overrides(self):
        cfg = config.load(env={
            "TERMINI_CHAOS_TH_OOM_RAM_PCT": "90.5",
            "TERMINI_CHAOS_CADENCE_MS": "1500",
            "TERMINI_CHAOS_DURATION_OOM": "5",
            "TERMINI_CHAOS_CONTROLLER_URL": "http://10.0.0.9:9999",
        })
        self.assertEqual(cfg["thresholds"]["oom_ram_pct"], 90.5)
        self.assertEqual(cfg["node"]["cadence_ms"], 1500)
        self.assertEqual(cfg["scenarios"]["oom"]["duration_s"], 5)
        self.assertEqual(config.controller_url(cfg), "http://10.0.0.9:9999")


class TestSeriesRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = config.load()

    def test_oom_series_fires_only_oom(self):
        ev = common.evaluate_series(
            _series(self.cfg, "oom", self.cfg["scenarios"]["oom"]["params"], 8), self.cfg["thresholds"]
        )
        self.assertTrue(ev["oom-guard-01"]["fired"])
        self.assertFalse(ev["disk-degrade-01"]["fired"])
        self.assertFalse(ev["latency-01"]["fired"])

    def test_disk_degrade_series_fires_only_disk(self):
        ev = common.evaluate_series(
            _series(self.cfg, "disk-degrade", self.cfg["scenarios"]["disk-degrade"]["params"], 10),
            self.cfg["thresholds"],
        )
        self.assertTrue(ev["disk-degrade-01"]["fired"])
        self.assertFalse(ev["oom-guard-01"]["fired"])

    def test_latency_series_fires_only_latency(self):
        ev = common.evaluate_series(
            _series(self.cfg, "latency", self.cfg["scenarios"]["latency"]["params"], 8), self.cfg["thresholds"]
        )
        self.assertTrue(ev["latency-01"]["fired"])
        self.assertFalse(ev["oom-guard-01"]["fired"])
        self.assertFalse(ev["disk-degrade-01"]["fired"])

    def test_healthy_series_fires_nothing(self):
        ev = common.evaluate_series(_series(self.cfg, "healthy", {}, 30), self.cfg["thresholds"])
        self.assertFalse(any(v["fired"] for v in ev.values()), ev)

    def test_samples_are_schema_shaped(self):
        s = _series(self.cfg, "oom", self.cfg["scenarios"]["oom"]["params"], 1)[0]
        for key in ("ts", "seq", "ram_total_bytes", "ram_used_bytes", "disk", "net",
                    "lxd_instances", "project_id", "hostname", "ip_local", "mac_address"):
            self.assertIn(key, s)
        # C1 identifier transforms the controller enforces
        self.assertRegex(s["hostname"], r"^[0-9a-f]{16}$")
        self.assertTrue(common.is_ulid(s["project_id"]))
        self.assertEqual(s["ip_local"], "10.0.0.0/24")
        # ram_used never exceeds ram_total (contract range 0..total)
        self.assertLessEqual(s["ram_used_bytes"], s["ram_total_bytes"])


class TestReferenceOperator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = config.load()

    def test_precedence_and_no_action_on_healthy(self):
        thr = self.cfg["thresholds"]
        self.assertIsNone(reference_operator.select_action(_series(self.cfg, "healthy", {}, 10), thr))
        oom = reference_operator.select_action(
            _series(self.cfg, "oom", self.cfg["scenarios"]["oom"]["params"], 8), thr
        )
        self.assertIsNotNone(oom)
        self.assertEqual(oom["rule"], "oom-guard-01")
        disk = reference_operator.select_action(
            _series(self.cfg, "disk-degrade", self.cfg["scenarios"]["disk-degrade"]["params"], 10), thr
        )
        self.assertEqual(disk["rule"], "disk-degrade-01")


class TestLxdStub(unittest.TestCase):
    def test_records_mutations_only(self):
        srv = lxd_stub.serve("127.0.0.1", 0, log_path=None)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        base = f"http://127.0.0.1:{port}"
        try:
            st, body = common.http_json("GET", f"{base}/1.0")
            self.assertEqual(st, 200)
            self.assertEqual(body["status"], "Success")
            st, _ = common.http_json("POST", f"{base}/1.0/instances/vm-1/state", {"action": "restart"})
            self.assertEqual(st, 200)
            st, cmds = common.http_json("GET", f"{base}/__chaos__/commands")
            self.assertEqual(st, 200)
            self.assertEqual(len(cmds["commands"]), 1)
            self.assertEqual(cmds["commands"][0]["instance"], "vm-1")
            self.assertEqual(cmds["commands"][0]["body"], {"action": "restart"})
            # a GET is recorded as a request but not as a command
            st, reqs = common.http_json("GET", f"{base}/__chaos__/requests")
            self.assertGreaterEqual(len(reqs["requests"]), 2)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
