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
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))

from app import fields as F  # noqa: E402

# Real-shaped captures of the sources the §4.12–§4.14 collectors read (see the
# README in that directory for the exact copy commands).
FIXTURE = REPO / "tests" / "fixtures" / "telemetry"


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
        # The builder reads `lxd_instances[]` off THIS node's LXD; never let a
        # unit test reach the build host's real daemon (the merged tree's whole
        # failure came from a reader falling through to it — see
        # tests/test_node_instance_inventory.py).
        saved = (tb.LXD_UNIX_SOCKETS, tb._lxd_instances_over_cli,
                 tb._INVENTORY_CLI_DEAD_UNTIL)
        tb.LXD_UNIX_SOCKETS = (
            str(pathlib.Path(tempfile.mkdtemp(prefix="termini-no-lxd-")) / "absent.socket"),)
        tb._lxd_instances_over_cli = lambda: (None, "lxc: not consulted in this test")
        tb._INVENTORY_CLI_DEAD_UNTIL = 0.0
        try:
            sample = tb.build_sample(cfg, 1)
        finally:
            (tb.LXD_UNIX_SOCKETS, tb._lxd_instances_over_cli,
             tb._INVENTORY_CLI_DEAD_UNTIL) = saved
        _clean, errors = validate_sample(sample)
        self.assertEqual(errors, [])
        self.assertIsInstance(sample["net"][0]["latency_ms"], float)
        self.assertIsInstance(sample["disk"][0]["write_latency_ms"], float)


class MetricExpansionFixtureTest(unittest.TestCase):
    """The §4.12–§4.14 collectors against the committed REAL-shaped fixture.

    `tests/fixtures/telemetry/` mirrors this host's `/proc`, `/sys` and cgroup
    hierarchy (raw files as they were printed — not hand-written samples), so the
    parsers meet the real shapes: PSI's `some`/`full` line kinds, the two-line
    /proc/net/snmp label/value table, a 17-field partition `stat`, and an LXD
    instance cgroup.
    """

    @classmethod
    def setUpClass(cls):
        cls.saved = (tb.PROC_ROOT, tb.SYS_ROOT, tb.CGROUP_ROOT)
        tb.PROC_ROOT = str(FIXTURE / "proc")
        tb.SYS_ROOT = str(FIXTURE / "sys")
        tb.CGROUP_ROOT = str(FIXTURE / "sys" / "fs" / "cgroup")

    @classmethod
    def tearDownClass(cls):
        tb.PROC_ROOT, tb.SYS_ROOT, tb.CGROUP_ROOT = cls.saved

    def test_pressure_reads_the_four_contract_scalars(self):
        self.assertEqual(
            tb.pressure_facts(),
            {"cpu_some_avg10": 0.49, "io_some_avg10": 3.68,
             "memory_some_avg10": 0.0, "memory_full_avg10": 0.0},
        )
        # `io` also carries a `full` line and `cpu` does not: the object stays at
        # the contract's four keys either way (an extra key would be rejected at
        # ingest, a missing one would read as "no stall").
        self.assertEqual(set(tb.pressure_facts()), set(F.PRESSURE_FIELDS))

    def test_a_measured_zero_is_reported_as_zero(self):
        # The whole point of omit-not-fabricate is that it cuts BOTH ways: a real
        # 0 (measured, no stall) is a measurement and must survive as 0.
        pressure = tb.pressure_facts()
        self.assertEqual(pressure["memory_some_avg10"], 0.0)
        self.assertEqual(pressure["memory_full_avg10"], 0.0)

    def test_memory_scalars(self):
        self.assertEqual(
            tb.memory_pressure_scalars(),
            {"oom_kill_count": 37, "pgmajfault_count": 2670664,
             "mem_available_bytes": 2313532 * 1024},
        )

    def test_transport_scalars(self):
        self.assertEqual(
            tb.transport_scalars(),
            {"tcp_retransmit_segs": 297110, "conntrack_count": 1785,
             "conntrack_max": 262144},
        )

    def test_retransmit_is_read_by_label_not_by_column(self):
        # A kernel that inserts a counter ahead of RetransSegs must not shift the
        # value this field reports. The fixture's real table is copied into a
        # scratch root with one extra label/value column injected after `Tcp:`.
        root = pathlib.Path(tempfile.mkdtemp(prefix="termini-snmp-"))
        (root / "net").mkdir()
        out_lines = []
        tcp_rows = 0
        for line in (FIXTURE / "proc" / "net" / "snmp").read_text().splitlines():
            if line.startswith("Tcp:"):
                tcp_rows += 1
                if tcp_rows == 1:      # the labels line
                    out_lines.append(line.replace("RtoAlgorithm",
                                                  "NewCounter RtoAlgorithm", 1))
                    continue
                out_lines.append(line.replace("Tcp: 1 ", "Tcp: 999999999 1 ", 1))
                continue
            out_lines.append(line)
        self.assertEqual(tcp_rows, 2)
        (root / "net" / "snmp").write_text("\n".join(out_lines) + "\n")
        saved = tb.PROC_ROOT
        tb.PROC_ROOT = str(root)
        try:
            self.assertEqual(tb.tcp_retransmit_segs(), 297110,
                             "the value must follow the RetransSegs LABEL")
        finally:
            tb.PROC_ROOT = saved
            shutil.rmtree(root, ignore_errors=True)

    def test_disk_io_comes_from_the_device_behind_the_mount(self):
        mapping = tb._mount_majmin()
        self.assertEqual(mapping.get("/"), "8:1")
        # field 9 = I/Os in flight, field 10 = ms spent doing I/O
        self.assertEqual(tb._block_io_of("/", mapping), (0, 62358882))

    def test_disk_row_carries_the_extension_fields(self):
        # Through `disk_facts` itself: the mount is a real one (so statvfs and
        # the fsync'd probe are real) while the device table comes from the
        # fixture, which is exactly how the two halves meet on a live node.
        saved = tb._DISK_MOUNTS
        tb._DISK_MOUNTS = (("/dev/root", "/"),)
        try:
            row = tb.disk_facts()[0]
        finally:
            tb._DISK_MOUNTS = saved
        self.assertEqual(row["io_in_flight"], 0)
        self.assertEqual(row["io_ticks_ms"], 62358882)
        self.assertGreaterEqual(row["inode_total"], 1)
        self.assertGreaterEqual(row["inode_used"], 0)

    def test_iface_error_counters(self):
        for counter in ("rx_errors", "tx_errors", "rx_dropped", "tx_dropped"):
            # A real 0 here means "measured, clean" — not None, which would mean
            # "this node could not look".
            self.assertEqual(tb._iface_counter("enp5s0", counter), 0)
        self.assertIsNone(tb._iface_counter("no-such-iface", "rx_errors"))

    def test_instance_cgroup_counters(self):
        self.assertEqual(
            tb.instance_cgroup_facts("demo-node-a"),
            {"oom_kill_count": 0, "pgmajfault_count": 104483},
        )

    def test_instance_cgroup_counters_ride_the_instance_row(self):
        # §4.14 lands on `lxd_instances[]` itself: one running instance (from the
        # fixture cgroup) must arrive with its counters attached.
        items = [{"name": "demo-node-a", "status": "Running",
                  "state": {"memory": {"usage": 224927744, "total": 1073741824},
                            "disk": {"root": {"usage": 0}},
                            "cpu": {"usage": 4163450661}}}]
        saved_socket = tb._lxd_instances_over_socket
        tb._lxd_instances_over_socket = lambda: (items, "fixture")
        try:
            rows = tb.lxd_instance_inventory({"node_name": "fixture"})
        finally:
            tb._lxd_instances_over_socket = saved_socket
            tb._CPU_PREV.clear()
            tb._INSTANCE_START_PREV.clear()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["oom_kill_count"], 0)
        self.assertEqual(rows[0]["pgmajfault_count"], 104483)
        self.assertEqual(set(rows[0]) - set(F.LXD_INSTANCE_FIELDS), set())

    def test_a_name_that_is_not_a_path_component_is_refused(self):
        # The instance name is untrusted input used to build a filesystem path.
        for bad in ("../../etc", "a/b", "", "x" * 100, None, 7):
            self.assertEqual(tb.instance_cgroup_facts(bad), {})


class MetricExpansionOmissionTest(unittest.TestCase):
    """Omit, never fabricate: no source ⇒ no field (never a `0`)."""

    def setUp(self):
        self.empty = tempfile.mkdtemp(prefix="termini-empty-root-")
        self.saved = (tb.PROC_ROOT, tb.SYS_ROOT, tb.CGROUP_ROOT)
        tb.PROC_ROOT = os.path.join(self.empty, "proc")
        tb.SYS_ROOT = os.path.join(self.empty, "sys")
        tb.CGROUP_ROOT = os.path.join(self.empty, "sys", "fs", "cgroup")

    def tearDown(self):
        tb.PROC_ROOT, tb.SYS_ROOT, tb.CGROUP_ROOT = self.saved
        shutil.rmtree(self.empty, ignore_errors=True)

    def test_pressure_object_is_absent_not_empty(self):
        # `{}` would be a different lie: it says "this node reports no pressure".
        self.assertIsNone(tb.pressure_facts())

    def test_scalars_are_absent_not_zero(self):
        self.assertEqual(tb.memory_pressure_scalars(), {})
        self.assertEqual(tb.transport_scalars(), {})
        self.assertIsNone(tb.tcp_retransmit_segs())
        self.assertEqual(tb.conntrack_facts(), {})

    def test_disk_and_iface_helpers_are_none(self):
        self.assertIsNone(tb._block_io_of("/", {}))
        self.assertIsNone(tb._iface_counter("enp5s0", "rx_errors"))
        self.assertEqual(tb._mount_majmin(), {})

    def test_instance_cgroup_directory_missing(self):
        self.assertEqual(tb.instance_cgroup_facts("demo-node-a"), {})

    def test_one_missing_psi_file_omits_the_whole_object(self):
        # A partially-filled object would read as "no stall" for the missing
        # resource — the exact "looks complete" failure the card names.
        root = pathlib.Path(tempfile.mkdtemp(prefix="termini-psi-"))
        (root / "pressure").mkdir()
        (root / "pressure" / "cpu").write_text(
            (FIXTURE / "proc" / "pressure" / "cpu").read_text())
        (root / "pressure" / "io").write_text(
            (FIXTURE / "proc" / "pressure" / "io").read_text())
        saved = tb.PROC_ROOT
        tb.PROC_ROOT = str(root)
        try:
            self.assertIsNone(tb.pressure_facts())
        finally:
            tb.PROC_ROOT = saved
            shutil.rmtree(root, ignore_errors=True)

    def test_a_conntrack_max_of_zero_is_not_shipped(self):
        # The contract's range for a table capacity is ≥ 1, so a file that says 0
        # cannot be reported; the count beside it still can.
        root = pathlib.Path(tempfile.mkdtemp(prefix="termini-conntrack-"))
        base = root / "sys" / "net" / "netfilter"
        base.mkdir(parents=True)
        (base / "nf_conntrack_count").write_text("5\n")
        (base / "nf_conntrack_max").write_text("0\n")
        saved = tb.PROC_ROOT
        tb.PROC_ROOT = str(root)
        try:
            self.assertEqual(tb.conntrack_facts(), {"conntrack_count": 5})
        finally:
            tb.PROC_ROOT = saved
            shutil.rmtree(root, ignore_errors=True)

    def test_build_sample_omits_the_groups_and_still_validates(self):
        # End to end: with none of the sources readable, the sample carries NONE
        # of the new fields — and is still a valid sample, because omission is
        # legal by contract and a fabricated `0` is not.
        from app.validate import validate_sample

        cfg = {"controller_url": "http://127.0.0.1:9", "node_name": "test-node",
               "iface": "nulliface", "role_hint": "compute"}
        saved = (tb.LXD_UNIX_SOCKETS, tb._lxd_instances_over_cli,
                 tb._INVENTORY_CLI_DEAD_UNTIL)
        tb.LXD_UNIX_SOCKETS = (
            str(pathlib.Path(tempfile.mkdtemp(prefix="termini-no-lxd-")) / "absent.socket"),)
        tb._lxd_instances_over_cli = lambda: (None, "not consulted in this test")
        tb._INVENTORY_CLI_DEAD_UNTIL = 0.0
        try:
            sample = tb.build_sample(cfg, 1)
        finally:
            (tb.LXD_UNIX_SOCKETS, tb._lxd_instances_over_cli,
             tb._INVENTORY_CLI_DEAD_UNTIL) = saved
        _clean, errors = validate_sample(sample)
        self.assertEqual(errors, [])
        for key in ("pressure", "oom_kill_count", "pgmajfault_count",
                    "mem_available_bytes", "tcp_retransmit_segs",
                    "conntrack_count", "conntrack_max"):
            self.assertNotIn(key, sample, f"{key} must be omitted, never 0")
        for row in sample["disk"]:
            self.assertNotIn("io_ticks_ms", row)
            self.assertNotIn("io_in_flight", row)
        for row in sample["net"]:
            for counter in ("rx_errors", "tx_errors", "rx_dropped", "tx_dropped"):
                self.assertNotIn(counter, row)


class MetricExpansionBoundTest(unittest.TestCase):
    """Every new group is bounded, and the bound is asserted here."""

    def test_shipped_probe_list_fits_its_own_cap(self):
        # The cap must never silently drop a mount the collector ships today.
        self.assertGreaterEqual(tb.DISK_MAX, len(tb._DISK_MOUNTS))

    def test_disk_array_is_capped_and_kept_in_probe_order(self):
        roots = [tempfile.mkdtemp(prefix="termini-mount-")
                 for _ in range(tb.DISK_MAX + 4)]
        saved = tb._DISK_MOUNTS
        tb._DISK_MOUNTS = tuple((f"/dev/probe{i}", p) for i, p in enumerate(roots))
        try:
            rows = tb.disk_facts()
        finally:
            tb._DISK_MOUNTS = saved
            for path in roots:
                shutil.rmtree(path, ignore_errors=True)
        self.assertEqual(len(rows), tb.DISK_MAX)
        self.assertEqual([row["path"] for row in rows],
                         [f"/dev/probe{i}" for i in range(tb.DISK_MAX)])

    def test_net_array_is_one_row_per_sample(self):
        self.assertEqual(tb.NET_MAX, 1)
        saved = tb.measure_controller_rtt_ms
        tb.measure_controller_rtt_ms = lambda cfg, timeout=None: 1.25
        try:
            rows = tb.net_facts({}, "lo")
        finally:
            tb.measure_controller_rtt_ms = saved
        self.assertEqual(len(rows), tb.NET_MAX)
        self.assertEqual(rows[0]["iface"], "lo")

    def test_the_pressure_object_is_fixed_at_four_keys(self):
        self.assertEqual(len(tb._PSI_SOURCES), len(F.PRESSURE_FIELDS))
        self.assertEqual(len({f for f, _r, _k in tb._PSI_SOURCES}),
                         len(F.PRESSURE_FIELDS))

    def test_lxd_inventory_is_not_truncated(self):
        # `lxd_instances[]` is the array the guardrail resolves a remediation
        # target through (§4.6): the §4.14 counters ride it, and it must never be
        # capped — dropping the tail would make real instances unreachable.
        names = [f"inst-{i:02d}" for i in range(12)]
        items = [{"name": name, "status": "Running",
                  "state": {"memory": {"usage": 1024, "total": 2048},
                            "disk": {"root": {"usage": 0}},
                            "cpu": {"usage": 1000}}}
                 for name in names]
        saved = tb._lxd_instances_over_socket
        tb._lxd_instances_over_socket = lambda: (items, "fixture")
        try:
            rows = tb.lxd_instance_inventory({"node_name": "fixture"})
        finally:
            tb._lxd_instances_over_socket = saved
            tb._CPU_PREV.clear()
            tb._INSTANCE_START_PREV.clear()
        self.assertEqual(len(rows), len(names))
        self.assertEqual(len({row["vm_id"] for row in rows}), len(names))


class MetricExpansionHostTest(unittest.TestCase):
    """Against THIS host's real /proc and /sys (property-shaped, not pinned).

    The fixture tests pin the parsing; these pin that a real node can produce the
    groups at all, and that a source that does not exist here produces nothing
    rather than a zero. Every assertion is conditional on the source existing, so
    the suite stays honest on a host (or CI runner) without conntrack, without
    PSI, or with a root filesystem that is not a plain block device.
    """

    def test_pressure_matches_the_source_when_present(self):
        if not os.path.exists("/proc/pressure/cpu"):
            self.assertIsNone(tb.pressure_facts(),
                              "no /proc/pressure ⇒ no pressure object")
            return
        pressure = tb.pressure_facts()
        self.assertIsNotNone(pressure)
        self.assertEqual(set(pressure), set(F.PRESSURE_FIELDS))
        for value in pressure.values():
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 100.0)

    def test_vmstat_scalars_match_the_source(self):
        scalars = tb.memory_pressure_scalars()
        if os.path.exists("/proc/vmstat"):
            self.assertIn("oom_kill_count", scalars)
            self.assertIn("pgmajfault_count", scalars)
        if os.path.exists("/proc/meminfo"):
            self.assertIn("mem_available_bytes", scalars)
            self.assertGreater(scalars["mem_available_bytes"], 0)

    def test_transport_scalars_match_the_source(self):
        scalars = tb.transport_scalars()
        if os.path.exists("/proc/net/snmp"):
            self.assertIn("tcp_retransmit_segs", scalars)
        if os.path.exists("/proc/sys/net/netfilter/nf_conntrack_count"):
            self.assertIn("conntrack_count", scalars)
            self.assertIn("conntrack_max", scalars)

    def test_every_collected_group_validates(self):
        from app.validate import validate_sample

        sample = {
            "seq": 1, "ts": "2026-10-16T21:00:00.000Z",
            "cpu_util_pct": 1.0, "ram_total_bytes": 1024, "ram_used_bytes": 512,
            "disk": tb.disk_facts(),
            "pressure": tb.pressure_facts(),
        }
        sample.update(tb.memory_pressure_scalars())
        sample.update(tb.transport_scalars())
        if sample["pressure"] is None:
            del sample["pressure"]
        _clean, errors = validate_sample(sample)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
