"""Metric expansion (t_98a685c1): §4.12–§4.14 contract + ingest enforcement.

The research card (t_cae0dfa8, `docs/research/infra-metric-inventory.md`) fixed
the field names for the recommended groups; this card turned them into a
contract (§4.12–§4.14 of `node-telemetry.md`) and enforced them in
`fields.py`/`validate.py`. These tests pin four properties:

* **accepted at its bound, rejected one step past (both directions)** — the
  pressure floats are bounded 0–100, the cumulative counters are ≥ 0, and the
  two capacity fields (`conntrack_max`, `inode_total`) are ≥ 1, so a validator
  that rejects everything is not a pass;
* **C2 scan at nesting depth** — a C2 field name smuggled inside `pressure{}` or
  a `disk[]`/`net[]`/`lxd_instances[]` entry fail-closes, and none of the new
  field names itself tokenises to a C2 term;
* **omit, don't fabricate** — a sample with none of the new fields is valid
  (omission is legal, so a node that cannot read a source ships nothing rather
  than a `0`), and the contract prose pins "omitted, never 0" per group;
* **bounds honesty** — the worst-case serialized size of every new field at its
  maximum magnitude stays under `METRIC_EXPANSION_MAX_BYTES`, and the extension
  adds no new array (so no new top-N bound exists to exceed).
"""
from __future__ import annotations

import json
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))

from app import fields as F  # noqa: E402
from app import governance  # noqa: E402
from app.validate import validate_sample  # noqa: E402

U64 = 18446744073709551615  # a 64-bit counter's maximum magnitude

PRESSURE = {
    "cpu_some_avg10": 4.86,
    "io_some_avg10": 0.39,
    "memory_some_avg10": 0.0,
    "memory_full_avg10": 0.0,
}

DISK_ENTRY = {
    "path": "/dev/sda", "total_bytes": 274877906944, "free_bytes": 214748364800,
    "write_latency_ms": 3.0, "mount_ro": False,
    "io_ticks_ms": 50829476, "io_in_flight": 1, "inode_used": 1127654,
    "inode_total": 12976128,
}

NET_ENTRY = {
    "iface": "eth0", "rx_bytes": 123000, "tx_bytes": 11000, "latency_ms": 0.8,
    "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0,
}

LXD_ENTRY = {
    "vm_id": "01M4349RPXWDC52ZDAVMPCMJSH", "state": "running",
    "cpu_util_pct": 12.0, "ram_used_bytes": 3328599654,
    "ram_limit_bytes": 4294967296, "oom_kill_count": 0, "pgmajfault_count": 103032,
}


def _sample(**extra) -> dict:
    sample = {
        "seq": 1000,
        "ts": "2026-10-04T07:49:00.000Z",
        "cpu_util_pct": 31.2,
        "ram_total_bytes": 34359738368,
        "ram_used_bytes": 19756849561,
    }
    sample.update(extra)
    return sample


class IngestAcceptsTest(unittest.TestCase):
    """A contract-valid sample carrying every new field passes ingest + C2 scan."""

    def test_full_expansion_accepted(self):
        sample = _sample(
            pressure=dict(PRESSURE),
            oom_kill_count=37, pgmajfault_count=2647724, mem_available_bytes=2092816 * 1024,
            tcp_retransmit_segs=295393, conntrack_count=854, conntrack_max=262144,
            disk=[dict(DISK_ENTRY)], net=[dict(NET_ENTRY)],
            lxd_instances=[dict(LXD_ENTRY)],
        )
        clean, errors = validate_sample(sample)
        self.assertEqual(errors, [])
        self.assertEqual(clean["pressure"]["cpu_some_avg10"], 4.86)
        self.assertEqual(clean["disk"][0]["inode_total"], 12976128)
        self.assertEqual(clean["lxd_instances"][0]["pgmajfault_count"], 103032)
        self.assertEqual(governance.scan_c2(clean), [])

    def test_new_fields_are_metric_fields(self):
        # A sample carrying only `pressure{}` (or any one new field) satisfies the
        # "at least one metric field" requirement.
        for key, value in (
            ("pressure", dict(PRESSURE)),
            ("oom_kill_count", 1),
            ("pgmajfault_count", 1),
            ("mem_available_bytes", 1),
            ("tcp_retransmit_segs", 1),
            ("conntrack_count", 1),
            ("conntrack_max", 1),
        ):
            _clean, errors = validate_sample({"seq": 1, "ts": "2026-10-04T07:49:00.000Z", key: value})
            self.assertEqual(errors, [], f"{key} should count as a metric field")


class BoundsTest(unittest.TestCase):
    """Accepted at the bound, rejected one step past — both directions."""

    def test_pressure_float_bounds(self):
        for field in ("cpu_some_avg10", "io_some_avg10",
                      "memory_some_avg10", "memory_full_avg10"):
            for ok in (0.0, 100.0, 100):
                clean, errors = validate_sample(_sample(pressure={field: ok}))
                self.assertEqual(errors, [], f"{field}={ok!r} at the bound must validate")
            for bad in (-0.1, 100.1, -1, 101):
                _clean, errors = validate_sample(_sample(pressure={field: bad}))
                self.assertTrue(errors, f"{field}={bad!r} one step past must reject")

    def test_pressure_type_is_number(self):
        for field in ("cpu_some_avg10", "memory_full_avg10"):
            _clean, errors = validate_sample(_sample(pressure={field: "high"}))
            self.assertTrue(errors, f"{field}='high' must reject")
            _clean, errors = validate_sample(_sample(pressure={field: True}))
            self.assertTrue(errors, f"{field}=True (bool) must reject")

    def test_counters_are_nonnegative(self):
        counters = ("oom_kill_count", "pgmajfault_count", "mem_available_bytes",
                    "tcp_retransmit_segs", "conntrack_count")
        for field in counters:
            _clean, errors = validate_sample(_sample(**{field: 0}))
            self.assertEqual(errors, [], f"{field}=0 at the bound must validate")
            _clean, errors = validate_sample(_sample(**{field: -1}))
            self.assertTrue(errors, f"{field}=-1 one step past must reject")

    def test_counter_type_is_integer(self):
        for field, bad in (("oom_kill_count", 1.5), ("oom_kill_count", "37"),
                           ("conntrack_count", True), ("mem_available_bytes", 3.0)):
            _clean, errors = validate_sample(_sample(**{field: bad}))
            self.assertTrue(errors, f"{field}={bad!r} must reject (not an int)")

    def test_capacity_fields_are_at_least_one(self):
        _clean, errors = validate_sample(_sample(conntrack_max=1))
        self.assertEqual(errors, [], "conntrack_max=1 at the bound must validate")
        _clean, errors = validate_sample(_sample(conntrack_max=0))
        self.assertTrue(errors, "conntrack_max=0 one step past must reject")
        clean, errors = validate_sample(_sample(disk=[dict(DISK_ENTRY, inode_total=1)]))
        self.assertEqual(errors, [])
        _clean, errors = validate_sample(_sample(disk=[dict(DISK_ENTRY, inode_total=0)]))
        self.assertTrue(errors, "inode_total=0 must reject")

    def test_nested_extension_fields_bounds(self):
        # disk[]/net[]/lxd_instances[] extension fields: accepted at 0, rejected at -1.
        cases = [
            ("disk", [dict(DISK_ENTRY, **{"io_ticks_ms": -1})]),
            ("disk", [dict(DISK_ENTRY, **{"io_in_flight": -1})]),
            ("disk", [dict(DISK_ENTRY, **{"inode_used": -1})]),
            ("net", [dict(NET_ENTRY, **{"rx_errors": -1})]),
            ("net", [dict(NET_ENTRY, **{"tx_dropped": -1})]),
            ("lxd_instances", [dict(LXD_ENTRY, **{"oom_kill_count": -1})]),
            ("lxd_instances", [dict(LXD_ENTRY, **{"pgmajfault_count": -1})]),
        ]
        for key, value in cases:
            _clean, errors = validate_sample(_sample(**{key: value}))
            self.assertTrue(errors, f"{key}={value!r} must reject")


class C2AtDepthTest(unittest.TestCase):
    """The C2 field-name scan reaches the new nesting levels."""

    def test_c2_name_nested_in_pressure_is_caught(self):
        raw = _sample(pressure=dict(PRESSURE, message="i/o error on /var/lib/termini"))
        self.assertTrue(governance.scan_c2(raw))

    def test_c2_name_nested_in_extended_array_entries_is_caught(self):
        for key, entry, c2key in (
            ("disk", dict(DISK_ENTRY, cmdline="/bin/sh -c x"),
             "cmdline"),
            ("disk", dict(DISK_ENTRY, logs=["2026-10-16 ERROR"]), "logs"),
            ("net", dict(NET_ENTRY, app_logs=["x"]), "app_logs"),
            ("lxd_instances", dict(LXD_ENTRY, customer_notes="x"), "customer_notes"),
        ):
            raw = _sample(**{key: [entry]})
            self.assertTrue(governance.scan_c2(raw),
                            f"{c2key} nested in {key}[] must be a C2 hit")

    def test_no_new_field_name_tokenises_to_c2(self):
        names = set(F.PRESSURE_FIELDS)
        names |= {"oom_kill_count", "pgmajfault_count", "mem_available_bytes",
                  "tcp_retransmit_segs", "conntrack_count", "conntrack_max"}
        names |= {"io_ticks_ms", "io_in_flight", "inode_used", "inode_total",
                  "rx_errors", "tx_errors", "rx_dropped", "tx_dropped"}
        for name in names:
            self.assertFalse(governance.is_c2_field_name(name),
                             f"{name} is a legitimate C0 field but reads as C2")


class OmitDontFabricateTest(unittest.TestCase):
    """Omission is legal and the contract says so — never a fabricated 0."""

    def test_omission_is_valid(self):
        # A sample with NONE of the new fields validates clean: a node that cannot
        # read any of the new sources is never forced to fabricate a value.
        clean, errors = validate_sample(_sample())
        self.assertEqual(errors, [])

    def test_each_group_is_optional(self):
        for key in ("pressure", "oom_kill_count", "mem_available_bytes",
                    "tcp_retransmit_segs", "conntrack_count", "conntrack_max"):
            clean, errors = validate_sample(_sample())
            self.assertNotIn(key, clean)
            self.assertEqual(errors, [])

    def test_contract_documents_omission_rule(self):
        doc = (REPO / "docs" / "contracts" / "node-telemetry.md").read_text()
        # Every new group states "omitted, never 0" (or the pressure equivalent).
        self.assertIn("never an empty `{}`", doc)                       # §4.12 pressure
        self.assertIn("omitted, never `0`", doc)                        # §4.12 scalars
        self.assertIn("when its `/sys` or `/proc` file", doc)           # §4.13
        self.assertIn("when the cgroup file", doc)                      # §4.14


class BoundsHonestyTest(unittest.TestCase):
    """Worst-case bytes are fixed and asserted; no new array is introduced."""

    def test_worst_case_bytes_under_budget(self):
        sample = _sample(
            pressure={k: 100.0 for k in F.PRESSURE_FIELDS},
            oom_kill_count=U64, pgmajfault_count=U64, mem_available_bytes=U64,
            tcp_retransmit_segs=U64, conntrack_count=U64, conntrack_max=U64,
            disk=[{"io_ticks_ms": U64, "io_in_flight": U64, "inode_used": U64, "inode_total": U64}],
            net=[{"rx_errors": U64, "tx_errors": U64, "rx_dropped": U64, "tx_dropped": U64}],
            lxd_instances=[{"oom_kill_count": U64, "pgmajfault_count": U64}],
        )
        encoded = json.dumps(sample, separators=(",", ":"))
        self.assertLessEqual(
            len(encoded), F.METRIC_EXPANSION_MAX_BYTES,
            f"worst-case new-field sample is {len(encoded)} bytes — exceeds the "
            f"{F.METRIC_EXPANSION_MAX_BYTES}-byte budget",
        )

    def test_pressure_is_a_fixed_object_not_an_array(self):
        spec = F.SAMPLE_CONTAINERS["pressure"]
        self.assertEqual(spec["kind"], "obj")
        self.assertEqual(set(F.PRESSURE_FIELDS),
                         {"cpu_some_avg10", "io_some_avg10",
                          "memory_some_avg10", "memory_full_avg10"})

    def test_extension_adds_no_new_array(self):
        # The new fields ride disk[]/net[]/lxd_instances[]; no new top-level array
        # appears, so no new top-N bound (and no new over-long array) exists.
        new_keys = {"pressure", "oom_kill_count", "pgmajfault_count",
                    "mem_available_bytes", "tcp_retransmit_segs",
                    "conntrack_count", "conntrack_max"}
        list_containers = {k for k, s in F.SAMPLE_CONTAINERS.items() if s["kind"] == "list"}
        self.assertFalse(new_keys & list_containers,
                         "no new field may introduce an unbounded array")

    def test_over_long_array_still_rejects(self):
        # Regression: the shared max_items mechanism (PROCESSES_MAX) is unchanged,
        # so an over-long bounded array still rejects rather than truncates.
        proc = {"pid": 1, "name": "x", "state": "running", "cpu_util_pct": 1.0,
                "ram_used_bytes": 1, "threads": 1, "age_sec": 1}
        rows = [dict(proc, pid=1000 + i) for i in range(F.PROCESSES_MAX + 1)]
        _clean, errors = validate_sample(_sample(processes=rows))
        self.assertTrue(any("exceeds the contract maximum" in e for e in errors), errors)


class ContractMirrorTest(unittest.TestCase):
    """Field names appear verbatim in the contract and in fields.py."""

    def test_new_fields_are_c0(self):
        for name in F.PRESSURE_FIELDS:
            self.assertEqual(F.PRESSURE_FIELDS[name]["cclass"], "C0")
        for name in ("oom_kill_count", "pgmajfault_count", "mem_available_bytes",
                     "tcp_retransmit_segs", "conntrack_count", "conntrack_max"):
            self.assertEqual(F.SAMPLE_FIELDS[name]["cclass"], "C0")
        for name in ("io_ticks_ms", "io_in_flight", "inode_used", "inode_total"):
            self.assertEqual(F.DISK_FIELDS[name]["cclass"], "C0")
        for name in ("rx_errors", "tx_errors", "rx_dropped", "tx_dropped"):
            self.assertEqual(F.NET_FIELDS[name]["cclass"], "C0")
        for name in ("oom_kill_count", "pgmajfault_count"):
            self.assertEqual(F.LXD_INSTANCE_FIELDS[name]["cclass"], "C0")

    def test_handoff_names_appear_verbatim_in_contract(self):
        doc = (REPO / "docs" / "contracts" / "node-telemetry.md").read_text()
        gov = (REPO / "docs" / "contracts" / "governance.md").read_text()
        names = [
            "cpu_some_avg10", "io_some_avg10", "memory_some_avg10", "memory_full_avg10",
            "oom_kill_count", "pgmajfault_count", "mem_available_bytes",
            "io_ticks_ms", "io_in_flight", "inode_used", "inode_total",
            "rx_errors", "tx_errors", "rx_dropped", "tx_dropped",
            "tcp_retransmit_segs", "conntrack_count", "conntrack_max",
        ]
        for name in names:
            self.assertIn(name, doc, f"{name} missing from node-telemetry.md")
            self.assertIn(name, gov, f"{name} missing from governance.md §4.1.1")

    def test_contract_sections_exist(self):
        doc = (REPO / "docs" / "contracts" / "node-telemetry.md").read_text()
        for section in ("### 4.12", "### 4.13", "### 4.14", "### 4.15"):
            self.assertIn(section, doc)


if __name__ == "__main__":
    unittest.main()
