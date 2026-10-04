"""Tests for the stateful trigger engine (hysteresis + debounce + persistence)."""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))

from app import db  # noqa: E402
from app.operator import detect, triggers  # noqa: E402
from app.operator import store as opstore  # noqa: E402

NOW = "2026-10-04T08:00:00.000Z"
RAM_TOTAL = 34_359_738_368
DISK_TOTAL = 274_877_906_944


def sample(seq, ts, *, ram_pct=None, mount_ro=False, latency_ms=0.8,
           free_pct=78.0, write_latency_ms=3.0) -> dict:
    s = {
        "seq": seq, "ts": ts,
        "ram_total_bytes": RAM_TOTAL,
        "ram_used_bytes": int(RAM_TOTAL * (ram_pct if ram_pct is not None else 50.0) / 100.0),
        "disk": [{"path": "/dev/sda", "total_bytes": DISK_TOTAL,
                  "free_bytes": int(DISK_TOTAL * free_pct / 100.0),
                  "write_latency_ms": write_latency_ms, "mount_ro": mount_ro}],
        "net": [{"iface": "eth0", "rx_bytes": 1, "tx_bytes": 1, "latency_ms": latency_ms}],
    }
    return s


class DebounceTest(unittest.TestCase):
    def test_single_noisy_sample_does_not_fire(self):
        th = detect.Thresholds()
        eng = triggers.TriggerEngine(th)
        series = [sample(1, "2026-10-04T08:00:01Z", ram_pct=97.0),  # one spike
                  sample(2, "2026-10-04T08:00:02Z", ram_pct=50.0)]
        events, _ = eng.evaluate("n1", series, {}, NOW)
        self.assertEqual([e for e in events if e.rule_id == "oom-guard-01"], [])

    def test_sustained_breach_fires_after_debounce(self):
        th = detect.Thresholds()
        eng = triggers.TriggerEngine(th)
        series = [sample(i + 1, f"2026-10-04T08:00:{i:02d}Z", ram_pct=97.0)
                  for i in range(3)]
        events, state = eng.evaluate("n1", series, {}, NOW)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].rule_id, "oom-guard-01")
        self.assertEqual(state["oom-guard-01"]["state"], "fired")
        self.assertEqual(state["oom-guard-01"]["fired_count"], 1)


class HysteresisTest(unittest.TestCase):
    def test_fired_latch_blocks_refire_within_band(self):
        th = detect.Thresholds()
        eng = triggers.TriggerEngine(th)
        # Fire at 97%.
        series = [sample(i + 1, f"2026-10-04T08:00:{i:02d}Z", ram_pct=97.0)
                  for i in range(3)]
        events, state = eng.evaluate("n1", series, {}, NOW)
        self.assertEqual(len(events), 1)

        # A recovering metric oscillating in the band (91-94%) must NOT re-fire.
        series = [sample(i + 1, f"2026-10-04T08:01:{i:02d}Z", ram_pct=93.0)
                  for i in range(3)]
        events, state = eng.evaluate("n1", series, state, "2026-10-04T08:01:03Z")
        self.assertEqual(events, [])
        self.assertEqual(state["oom-guard-01"]["state"], "fired")

    def test_rearm_after_clear_then_refire_new_episode(self):
        th = detect.Thresholds()
        eng = triggers.TriggerEngine(th)
        series = [sample(i + 1, f"2026-10-04T08:00:{i:02d}Z", ram_pct=97.0)
                  for i in range(3)]
        _, state = eng.evaluate("n1", series, {}, NOW)

        # Recover below the clear threshold (<=90%) for clear_consecutive samples.
        series = [sample(i + 1, f"2026-10-04T08:01:{i:02d}Z", ram_pct=50.0)
                  for i in range(3)]
        events, state = eng.evaluate("n1", series, state, "2026-10-04T08:01:03Z")
        self.assertEqual(events, [])
        self.assertEqual(state["oom-guard-01"]["state"], "armed")

        # Re-breach -> new episode (episode 2).
        series = [sample(i + 1, f"2026-10-04T08:02:{i:02d}Z", ram_pct=97.0)
                  for i in range(3)]
        events, state = eng.evaluate("n1", series, state, "2026-10-04T08:02:03Z")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].episode, 2)

    def test_partial_recovery_does_not_rearm(self):
        th = detect.Thresholds()
        eng = triggers.TriggerEngine(th)
        series = [sample(i + 1, f"2026-10-04T08:00:{i:02d}Z", ram_pct=97.0)
                  for i in range(3)]
        _, state = eng.evaluate("n1", series, {}, NOW)
        # Only 2 healthy samples (need 3) -> still latched.
        series = [sample(i + 1, f"2026-10-04T08:01:{i:02d}Z", ram_pct=50.0)
                  for i in range(2)]
        events, state = eng.evaluate("n1", series, state, "2026-10-04T08:01:02Z")
        self.assertEqual(state["oom-guard-01"]["state"], "fired")


class DiskFailTest(unittest.TestCase):
    def test_mount_ro_fires_immediately(self):
        th = detect.Thresholds()
        eng = triggers.TriggerEngine(th)
        series = [sample(1, "2026-10-04T08:00:01Z", mount_ro=True)]  # single sample
        events, state = eng.evaluate("n1", series, {}, NOW)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].rule_id, "disk-fail-01")
        self.assertEqual(events[0].type, "disk_fail")
        self.assertEqual(state["disk-fail-01"]["state"], "fired")

    def test_rw_disk_does_not_fire(self):
        th = detect.Thresholds()
        eng = triggers.TriggerEngine(th)
        series = [sample(i + 1, f"2026-10-04T08:00:{i:02d}Z", mount_ro=False)
                  for i in range(5)]
        events, _ = eng.evaluate("n1", series, {}, NOW)
        self.assertEqual([e for e in events if e.rule_id == "disk-fail-01"], [])


class LatencyTest(unittest.TestCase):
    def test_detection_latency_is_measured(self):
        th = detect.Thresholds()
        eng = triggers.TriggerEngine(th)
        # Breach starts at 08:00:00, fires at 08:00:02 (3rd sample).
        series = [sample(i + 1, f"2026-10-04T08:00:0{i}Z", ram_pct=97.0)
                  for i in range(3)]
        events, _ = eng.evaluate("n1", series, {}, "2026-10-04T08:00:02.000Z")
        self.assertEqual(events[0].breach_started_at, "2026-10-04T08:00:00Z")
        lat = triggers.detection_latency_s(events[0])
        self.assertAlmostEqual(lat, 2.0, delta=0.01)


class PersistenceTest(unittest.TestCase):
    def test_split_merge_roundtrip(self):
        th = detect.Thresholds(oom_ram_pct=88.0, disk_fail_consecutive=2)
        merged = triggers.merge_config(triggers.split_config(th))
        self.assertEqual(merged.oom_ram_pct, 88.0)
        self.assertEqual(merged.disk_fail_consecutive, 2)

    def test_trigger_state_roundtrip(self):
        tmp = tempfile.mkdtemp(prefix="termini-trig-")
        db_path = os.path.join(tmp, "test.db")
        db.migrate(db_path)
        with db.session(db_path) as conn:
            opstore.save_trigger_config(conn, "oom-guard-01",
                                        {"oom_ram_pct": 91.0}, "runtime", NOW)
            opstore.save_trigger_state(conn, "n1", "oom-guard-01",
                                       {"state": "fired", "last_fired_at": NOW,
                                        "last_cleared_at": None, "fired_count": 3}, NOW)
        with db.session(db_path) as conn:
            self.assertEqual(opstore.load_trigger_configs(conn)["oom-guard-01"]["oom_ram_pct"], 91.0)
            st = opstore.load_trigger_state(conn, "n1")["oom-guard-01"]
            self.assertEqual(st["state"], "fired")
            self.assertEqual(st["fired_count"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
