"""Tests for the detector (threshold evaluation) and playbook chooser."""
from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))

from app import ulid  # noqa: E402
from app.operator import detect, playbook  # noqa: E402

NOW = "2026-10-04T08:00:00Z"
RAM_TOTAL = 34_359_738_368
DISK_TOTAL = 274_877_906_944


def sample(seq: int, ts: str, *, ram_used: int | None = None,
           disk_free: int | None = None, write_latency_ms: float = 3.0,
           latency_ms: float = 0.8, instances: list | None = None) -> dict:
    s = {
        "seq": seq, "ts": ts,
        "ram_total_bytes": RAM_TOTAL,
        "ram_used_bytes": ram_used if ram_used is not None else RAM_TOTAL // 2,
        "disk": [{"path": "/dev/sda", "total_bytes": DISK_TOTAL,
                  "free_bytes": disk_free if disk_free is not None else DISK_TOTAL * 78 // 100,
                  "write_latency_ms": write_latency_ms, "mount_ro": False}],
        "net": [{"iface": "eth0", "rx_bytes": 1, "tx_bytes": 1, "latency_ms": latency_ms}],
    }
    if instances is not None:
        s["lxd_instances"] = instances
    return s


def oom_series(n=3) -> list[dict]:
    return [sample(100 + i, f"2026-10-04T08:00:{i:02d}Z", ram_used=34_000_000_000)
            for i in range(n)]


def healthy_series(n=3) -> list[dict]:
    return [sample(100 + i, f"2026-10-04T08:00:{i:02d}Z") for i in range(n)]


class OomDetectTest(unittest.TestCase):
    def test_sustained_oom_fires(self):
        ev = detect.evaluate("n1", oom_series(3), detect.Thresholds(), NOW)
        oom = [e for e in ev if e.type == "oom"]
        self.assertEqual(len(oom), 1)
        self.assertEqual(oom[0].rule_id, "oom-guard-01")
        self.assertGreaterEqual(oom[0].value, 95.0)

    def test_healthy_does_not_fire(self):
        ev = detect.evaluate("n1", healthy_series(3), detect.Thresholds(), NOW)
        self.assertEqual([e for e in ev if e.type == "oom"], [])

    def test_single_spike_below_sustain_does_not_fire(self):
        series = healthy_series(3)
        series[-1]["ram_used_bytes"] = 34_000_000_000  # one spike only
        ev = detect.evaluate("n1", series, detect.Thresholds(oom_consecutive=2), NOW)
        self.assertEqual([e for e in ev if e.type == "oom"], [])

    def test_custom_threshold(self):
        # 58% used should NOT trip the default 95% threshold but WILL trip a 50% one.
        series = [sample(100 + i, f"2026-10-04T08:00:{i:02d}Z",
                         ram_used=20_000_000_000) for i in range(3)]
        self.assertEqual([e for e in detect.evaluate("n", series, detect.Thresholds(), NOW)
                          if e.type == "oom"], [])
        fired = [e for e in detect.evaluate("n", series, detect.Thresholds(oom_ram_pct=50.0), NOW)
                 if e.type == "oom"]
        self.assertEqual(len(fired), 1)


class DiskDetectTest(unittest.TestCase):
    def test_low_free_disk_fires(self):
        series = [sample(100 + i, f"2026-10-04T08:00:{i:02d}Z", disk_free=5_000_000_000)
                  for i in range(4)]
        ev = detect.evaluate("n1", series, detect.Thresholds(), NOW)
        disk = [e for e in ev if e.type == "disk_degrade"]
        self.assertEqual(len(disk), 1)
        self.assertEqual(disk[0].rule_id, "disk-degrade-01")

    def test_write_latency_fires(self):
        series = [sample(100 + i, f"2026-10-04T08:00:{i:02d}Z", write_latency_ms=80.0)
                  for i in range(6)]
        ev = detect.evaluate("n1", series, detect.Thresholds(), NOW)
        disk = [e for e in ev if e.type == "disk_degrade"]
        self.assertEqual(len(disk), 1)

    def test_steep_negative_slope_fires(self):
        # free disk falls 20 GB over 5 seconds (~4 GB/s, far steeper than -5 MB/s),
        # but never crosses the 5% free floor, isolating the slope signal.
        series = [sample(100 + i, f"2026-10-04T08:00:{i:02d}Z",
                         disk_free=100_000_000_000 - i * 4_000_000_000)
                  for i in range(6)]
        ev = detect.evaluate("n1", series, detect.Thresholds(), NOW)
        disk = [e for e in ev if e.type == "disk_degrade"]
        self.assertEqual(len(disk), 1)

    def test_healthy_disk_does_not_fire(self):
        ev = detect.evaluate("n1", healthy_series(6), detect.Thresholds(), NOW)
        self.assertEqual([e for e in ev if e.type == "disk_degrade"], [])


class LatencyDetectTest(unittest.TestCase):
    def test_sustained_latency_fires(self):
        series = [sample(100 + i, f"2026-10-04T08:00:{i:02d}Z", latency_ms=600.0)
                  for i in range(4)]
        ev = detect.evaluate("n1", series, detect.Thresholds(), NOW)
        lat = [e for e in ev if e.type == "latency"]
        self.assertEqual(len(lat), 1)
        self.assertEqual(lat[0].rule_id, "latency-01")

    def test_low_latency_does_not_fire(self):
        ev = detect.evaluate("n1", healthy_series(6), detect.Thresholds(), NOW)
        self.assertEqual([e for e in ev if e.type == "latency"], [])


class PlaybookChooseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playbooks = playbook.load_playbooks(str(REPO / "controller" / "playbooks"))

    def test_oom_chooses_restart_heaviest(self):
        ev = [e for e in detect.evaluate("n1", oom_series(3), detect.Thresholds(), NOW)
              if e.type == "oom"][0]
        instances = [
            {"vm_id": ulid.new_ulid(), "state": "running", "ram_used_bytes": 2_000_000_000},
            {"vm_id": ulid.new_ulid(), "state": "running", "ram_used_bytes": 9_000_000_000},
        ]
        ev.evidence[-1]["lxd_instances"] = instances
        action = playbook.choose(ev, self.playbooks, instance_names={},
                                 default_instance_name="agent")
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, "instance.restart")
        self.assertEqual(action.params["instance_name"], instances[1]["vm_id"])  # heaviest

    def test_instance_name_mapping(self):
        ev = [e for e in detect.evaluate("n1", oom_series(3), detect.Thresholds(), NOW)
              if e.type == "oom"][0]
        vm_id = ulid.new_ulid()
        ev.evidence[-1]["lxd_instances"] = [
            {"vm_id": vm_id, "state": "running", "ram_used_bytes": 5_000_000_000}
        ]
        action = playbook.choose(ev, self.playbooks, instance_names={vm_id: "agent-c1"},
                                 default_instance_name="agent")
        self.assertEqual(action.params["instance_name"], "agent-c1")

    def test_disk_chooses_drain(self):
        series = [sample(100 + i, f"2026-10-04T08:00:{i:02d}Z", disk_free=5_000_000_000)
                  for i in range(4)]
        ev = [e for e in detect.evaluate("n1", series, detect.Thresholds(), NOW)
              if e.type == "disk_degrade"][0]
        action = playbook.choose(ev, self.playbooks)
        self.assertEqual(action.kind, "instance.stop")

    def test_latency_chooses_restart(self):
        series = [sample(100 + i, f"2026-10-04T08:00:{i:02d}Z", latency_ms=600.0)
                  for i in range(4)]
        ev = [e for e in detect.evaluate("n1", series, detect.Thresholds(), NOW)
              if e.type == "latency"][0]
        action = playbook.choose(ev, self.playbooks)
        self.assertEqual(action.kind, "instance.restart")

    def test_unknown_type_returns_none(self):
        from app.operator.models import HealthEvent
        ev = HealthEvent(event_id=ulid.new_ulid(), node_id="n1", rule_id="x",
                         type="unknown", metric="m", value=0.0, threshold="t",
                         fired_at=NOW)
        self.assertIsNone(playbook.choose(ev, self.playbooks))


if __name__ == "__main__":
    unittest.main(verbosity=2)
