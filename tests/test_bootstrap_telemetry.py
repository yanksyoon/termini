"""Regression tests for the node bootstrap telemetry builder (t_159722ab).

W7 gate finding G1 (t_8d243ee1): `infra/bootstrap/node/termini-bootstrap.py`
reported a *literal* `net[].latency_ms = 0.3` and `disk[].write_latency_ms = 0.5`,
so the controller's `latency-01` rule (net latency >= 250 ms) could never fire and
the fan-network failure mode was undetectable. These tests pin the measured
behaviour: real round-trips to the controller and real fsync'd writes, with the
C0 field types unchanged.
"""
from __future__ import annotations

import importlib.util
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))


def _load_bootstrap():
    path = REPO / "infra" / "bootstrap" / "node" / "termini-bootstrap.py"
    spec = importlib.util.spec_from_file_location("termini_bootstrap_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tb = _load_bootstrap()


class ControllerRttTest(unittest.TestCase):
    """`net[].latency_ms` is the round-trip to the controller (contract §4.4)."""

    @classmethod
    def setUpClass(cls):
        cls.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cls.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        cls.srv.bind(("127.0.0.1", 0))
        cls.srv.listen(16)
        cls.port = cls.srv.getsockname()[1]
        cls._stop = threading.Event()

        def loop():
            cls.srv.settimeout(0.2)
            while not cls._stop.is_set():
                try:
                    conn, _ = cls.srv.accept()
                    conn.close()
                except OSError:
                    pass

        cls._thread = threading.Thread(target=loop, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls):
        cls._stop.set()
        cls.srv.close()

    def test_rtt_is_a_measured_float(self):
        rtt = tb.measure_controller_rtt_ms({"controller_url": f"http://127.0.0.1:{self.port}"})
        self.assertIsInstance(rtt, float)
        self.assertGreaterEqual(rtt, 0.0)

    def test_ping_output_is_parsed(self):
        fake = subprocess.CompletedProcess(
            ["ping"], 0,
            stdout="64 bytes from 1.2.3.4: icmp_seq=1 ttl=64 time=301 ms\n",
            stderr="")
        original = tb.subprocess.run
        tb.subprocess.run = lambda *a, **k: fake
        try:
            self.assertEqual(tb._icmp_rtt_ms("1.2.3.4"), 301.0)
        finally:
            tb.subprocess.run = original

    def test_unreachable_controller_is_none_not_a_constant(self):
        # 192.0.2.0/24 is TEST-NET-1 (RFC 5737): never routed.
        cfg = {"controller_url": "http://192.0.2.1:8618"}
        self.assertIsNone(tb.measure_controller_rtt_ms(cfg, timeout=1.0))

    def test_net_facts_reports_the_measurement(self):
        net = tb.net_facts({"controller_url": f"http://127.0.0.1:{self.port}"}, "eth0")
        self.assertIsInstance(net[0]["latency_ms"], float)
        # the frozen defect: a literal constant instead of a live measurement
        self.assertNotEqual(net[0]["latency_ms"], 0.3)


class DiskProbeTest(unittest.TestCase):
    """`disk[].write_latency_ms` / `mount_ro` come from a real fsync'd write."""

    def test_write_latency_is_a_measured_float(self):
        with tempfile.TemporaryDirectory() as d:
            latency, mount_ro = tb._probe_disk(d)
        self.assertIsInstance(latency, float)
        self.assertGreaterEqual(latency, 0.0)
        self.assertFalse(mount_ro)

    def test_write_latency_tracks_real_io_time(self):
        with tempfile.TemporaryDirectory() as d:
            original = tb.os.fsync

            def slow_fsync(fd):
                time.sleep(0.05)
                return original(fd)

            tb.os.fsync = slow_fsync
            try:
                latency, _ = tb._probe_disk(d)
            finally:
                tb.os.fsync = original
        self.assertGreaterEqual(latency, 40.0)

    def test_disk_facts_measured_not_constant(self):
        with tempfile.TemporaryDirectory() as d:
            original = tb._DISK_MOUNTS
            tb._DISK_MOUNTS = (("/dev/root", d),)
            try:
                disk = tb.disk_facts()
            finally:
                tb._DISK_MOUNTS = original
        self.assertIsInstance(disk[0]["write_latency_ms"], float)
        self.assertNotEqual(disk[0]["write_latency_ms"], 0.5)
        self.assertFalse(disk[0]["mount_ro"])


class SchemaTest(unittest.TestCase):
    """The fields stay C0-class floats, accepted by the controller validator."""

    def test_sample_still_matches_frozen_schema(self):
        from app.validate import validate_sample

        cfg = {"controller_url": "http://127.0.0.1:9", "node_name": "test-node",
               "iface": "eth0", "role_hint": "compute"}
        sample = tb.build_sample(cfg, 1)
        _clean, errors = validate_sample(sample)
        self.assertEqual(errors, [])
        self.assertIsInstance(sample["net"][0]["latency_ms"], float)
        self.assertIsInstance(sample["disk"][0]["write_latency_ms"], float)


if __name__ == "__main__":
    unittest.main()
