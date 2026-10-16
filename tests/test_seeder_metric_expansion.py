#!/usr/bin/env python3
"""The demo seeder carries the §4.12–§4.14 metric groups in its synthetic samples.

FOLLOW-UP to the node-collector card (PR #190). The node daemon emits the
metric-expansion groups from real `/proc`/`/sys`/cgroup/LXD sources, but the demo
stream is synthesised by `console/seed_demo.py`, which built its own sample dict
and carried none of them. Without `pressure{}` the dashboard had nothing to chart
and the AI diagnosis never saw the single highest-value signal in a live window.

This file pins what the extension must do, so a regression is a failing build
rather than a silent gap:

* **emitted, valid, C2-clean** — every §4.12–§4.14 field the seeder emits
  validates under `controller/app/validate.py`, scans clean under
  `app.governance.scan_c2`, and survives `clean == raw` (no invented field, so
  nothing is silently stripped by the deny-by-default stripper);
* **the fault narrative** — a disk-degrade window pushes `io_some_avg10` up and
  keeps `memory_full_avg10` flat; a memory-pressure window pushes
  `memory_some_avg10`/`memory_full_avg10` up and keeps `io_some_avg10` flat. The
  cumulative counters advance only under the fault that names them (disk I/O time
  climbs under disk-degrade; OOM/major-fault counters advance under
  memory-pressure), and never run backwards across a watchdog respawn;
* **the ticker emits it live** — driving `NodeTicker._tick` produces a sample that
  validates and carries the new groups, not just `sample()` in isolation.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER = REPO / "controller"
sys.path.insert(0, str(CONTROLLER))
sys.path.insert(0, str(REPO))

from app import governance  # noqa: E402
from app.validate import validate_sample  # noqa: E402

# A controller-issued, valid 26-char ULID (the shape the seeder is handed back
# from `register`). `validate_sample` requires `project_id` to be ULID-shaped.
PROJECT_ID = "01M539DJ5WDFPAH3NR9NNXXVS2"


def _load_by_path(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SEEDER = _load_by_path("seed_demo_metric_expansion", REPO / "console" / "seed_demo.py")

PRESSURE_KEYS = {"cpu_some_avg10", "io_some_avg10",
                 "memory_some_avg10", "memory_full_avg10"}
FLAT_SCALARS = ("oom_kill_count", "pgmajfault_count", "mem_available_bytes",
                "tcp_retransmit_segs", "conntrack_count", "conntrack_max")
DISK_EXT = ("io_ticks_ms", "io_in_flight", "inode_used", "inode_total")
NET_EXT = ("rx_errors", "tx_errors", "rx_dropped", "tx_dropped")
INSTANCE_EXT = ("oom_kill_count", "pgmajfault_count")


def _sample(fault, **counters) -> dict:
    """One seeder sample for a given fault window, with realistic counters."""
    return SEEDER.sample(
        "demo-node-c", PROJECT_ID, 100,
        ram_used_bytes=66000000000, disk_write_latency_ms=120.0,
        latency_ms=0.8, uptime_sec=3600,
        vm_ids=[SEEDER.vm_id_for_instance("demo-workload"),
                SEEDER.vm_id_for_instance("demo-workload-2")],
        fault=fault,
        oom_kill_count=counters.get("oom_kill_count", 7),
        pgmajfault_count=counters.get("pgmajfault_count", 2647724),
        tcp_retransmit_segs=counters.get("tcp_retransmit_segs", 295393),
        io_ticks_ms=counters.get("io_ticks_ms", 50829476),
        instance_oom_kill_count=counters.get("instance_oom_kill_count", 2),
        instance_pgmajfault_count=counters.get("instance_pgmajfault_count", 103032),
    )


class SeederEmitsNewGroupsTest(unittest.TestCase):
    """Every §4.12–§4.14 field is present, valid, and C2-clean."""

    def test_sample_validates_and_is_c2_clean_for_every_fault_window(self):
        for fault in (None, SEEDER.FAULT_DISK, SEEDER.FAULT_MEMORY):
            raw = _sample(fault)
            clean, errors = validate_sample(raw)
            self.assertEqual(errors, [], f"fault={fault!r}: {errors}")
            self.assertEqual(governance.scan_c2(raw), [],
                             f"fault={fault!r}: C2 signature in the emitted sample")
            self.assertEqual(governance.check_c1_transforms(raw), [])
            # No invented field: the deny-by-default stripper must not have to
            # drop anything, so the cleaned sample is byte-for-byte the emitted one.
            self.assertEqual(clean, raw)

    def test_new_groups_are_present_in_the_emitted_shape(self):
        s = _sample(SEEDER.FAULT_MEMORY)
        self.assertEqual(set(s["pressure"]), PRESSURE_KEYS)
        for key in FLAT_SCALARS:
            self.assertIn(key, s, f"flat scalar {key} missing")
        for key in DISK_EXT:
            self.assertIn(key, s["disk"][0], f"disk[].{key} missing")
        for key in NET_EXT:
            self.assertIn(key, s["net"][0], f"net[].{key} missing")
        for key in INSTANCE_EXT:
            self.assertIn(key, s["lxd_instances"][0], f"lxd_instances[].{key} missing")

    def test_pressure_is_a_fixed_four_key_object(self):
        # The contract pins pressure{} at exactly four keys; an extra key would be
        # stripped by validation (and `clean == raw` above would fail).
        s = _sample(SEEDER.FAULT_MEMORY)
        self.assertEqual(len(s["pressure"]), 4)


class SeederFaultNarrativeTest(unittest.TestCase):
    """The values name the stalling resource; they never lie about it."""

    def test_disk_degrade_pushes_io_pressure_and_keeps_memory_flat(self):
        s = _sample(SEEDER.FAULT_DISK)
        p = s["pressure"]
        self.assertGreater(p["io_some_avg10"], 0.0)
        self.assertEqual(p["memory_some_avg10"], 0.0)
        self.assertEqual(p["memory_full_avg10"], 0.0)
        # A saturated device reads as a saturated device.
        self.assertGreater(s["disk"][0]["io_in_flight"], 0)
        # mem_available stays comfortably high: a disk fault is not memory
        # exhaustion and must not read as one.
        self.assertGreater(s["mem_available_bytes"], 16 * 1024 ** 3)

    def test_memory_pressure_pushes_memory_pressure_and_keeps_io_flat(self):
        s = _sample(SEEDER.FAULT_MEMORY)
        p = s["pressure"]
        self.assertGreater(p["memory_some_avg10"], 0.0)
        self.assertGreater(p["memory_full_avg10"], 0.0)
        self.assertEqual(p["io_some_avg10"], 0.0)
        # mem_available craters: the kernel's own "would swap" estimate.
        self.assertLess(s["mem_available_bytes"], 1 * 1024 ** 3)

    def test_healthy_window_is_quiet(self):
        s = _sample(None)
        self.assertEqual(s["pressure"], {k: 0.0 for k in PRESSURE_KEYS})
        self.assertEqual(s["disk"][0]["io_in_flight"], 0)


class SeederCounterMonotonicityTest(unittest.TestCase):
    """Cumulative counters advance only under their fault and never run backwards."""

    def _ticker(self, high_ram: bool):
        return SEEDER.NodeTicker(
            "http://127.0.0.1:1", {"node_id": "demo-node-c", "node_token": "tok"},
            PROJECT_ID, period_s=6.0, phase=0.0, high_ram=high_ram)

    def test_disk_fault_advances_io_ticks_but_never_ooms(self):
        disk = self._ticker(high_ram=False)
        for _ in range(24):
            disk._advance_counters(SEEDER.FAULT_DISK)
        self.assertGreater(disk.io_ticks_ms, 0)
        # Disk-degrade must not fabricate OOM kills — that would lie about which
        # resource is stalling.
        self.assertEqual(disk.oom_kills, 0)
        self.assertEqual(disk.instance_oom_kills, 0)

    def test_memory_fault_advances_oom_and_instance_faults(self):
        mem = self._ticker(high_ram=True)
        for _ in range(24):
            mem._advance_counters(SEEDER.FAULT_MEMORY)
        self.assertGreater(mem.oom_kills, 0)
        self.assertGreater(mem.pgmajfaults, 0)
        self.assertGreater(mem.instance_oom_kills, 0)
        self.assertGreater(mem.instance_pgmajfaults, 0)

    def test_respawn_inherits_the_counters(self):
        t = self._ticker(high_ram=True)
        for _ in range(10):
            t._advance_counters(SEEDER.FAULT_MEMORY)
        fresh = t.respawn()
        # A respawned ticker must not emit counters that run backwards.
        self.assertEqual(fresh.oom_kills, t.oom_kills)
        self.assertEqual(fresh.pgmajfaults, t.pgmajfaults)
        self.assertEqual(fresh.io_ticks_ms, t.io_ticks_ms)
        self.assertEqual(fresh.instance_oom_kills, t.instance_oom_kills)
        self.assertEqual(fresh.instance_pgmajfaults, t.instance_pgmajfaults)


class SeederTickerEmitsLiveTest(unittest.TestCase):
    """`NodeTicker._tick` itself produces a validating, C2-clean, new-group sample."""

    @staticmethod
    def _tick_sample(high_ram: bool, phase_frac: float) -> dict:
        """Drive one tick at a deterministic phase (0.25 = hot, 0.75 = healthy)."""
        t = SEEDER.NodeTicker(
            "http://127.0.0.1:1", {"node_id": "demo-node-c", "node_token": "tok"},
            PROJECT_ID, period_s=6.0, phase=0.0, high_ram=high_ram,
            gpu=SEEDER.GPU_TELEMETRY)
        captured: list[dict] = []
        t.send = lambda s: (captured.append(s), True)[1]
        t._tick(time.monotonic() - phase_frac * 6.0)
        return captured[0]

    def test_memory_node_hot_tick_emits_memory_pressure_and_validates(self):
        s = self._tick_sample(high_ram=True, phase_frac=0.25)
        self.assertGreater(s["pressure"]["memory_some_avg10"], 0.0)
        clean, errors = validate_sample(s)
        self.assertEqual(errors, [], errors)
        self.assertEqual(governance.scan_c2(clean), [])
        for key in FLAT_SCALARS:
            self.assertIn(key, s)

    def test_compute_node_hot_tick_emits_disk_pressure_and_validates(self):
        s = self._tick_sample(high_ram=False, phase_frac=0.25)
        self.assertGreater(s["pressure"]["io_some_avg10"], 0.0)
        self.assertEqual(s["pressure"]["memory_full_avg10"], 0.0)
        clean, errors = validate_sample(s)
        self.assertEqual(errors, [], errors)
        self.assertEqual(governance.scan_c2(clean), [])

    def test_healthy_tick_emits_flat_pressure(self):
        s = self._tick_sample(high_ram=True, phase_frac=0.75)
        self.assertEqual(s["pressure"], {k: 0.0 for k in PRESSURE_KEYS})


if __name__ == "__main__":
    unittest.main(verbosity=2)
