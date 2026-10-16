"""Deeper node telemetry (t_957956e5): `processes[]` and the operational fields.

Two directions are pinned here, and both matter:

* **accepted** — a contract-valid sample carrying `processes[]`, the §4.8
  operational fields (`restart_count` / `last_exit_code` / `state_age_sec`) and
  the §4.11 journal *counts* passes ingest validation and the C2 scan;
* **rejected** — `argv`, `cmdline`, `env` (or any other C2 field name) nested
  inside a `processes[]` or `services[]` entry still fail-closes the batch, and
  an over-long `processes[]` is a validation failure rather than a silent
  truncation.

The node-side collector is exercised against the real `/proc` of whatever host
runs the suite, so a change that emits a key outside the contract fails here
instead of at an incident.
"""
from __future__ import annotations

import importlib.util
import itertools
import pathlib
import sys
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))

from app import fields as F  # noqa: E402
from app import governance  # noqa: E402
from app.validate import validate_sample  # noqa: E402


def _load_bootstrap():
    path = REPO / "infra" / "bootstrap" / "node" / "termini-bootstrap.py"
    spec = importlib.util.spec_from_file_location("termini_bootstrap_processes", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tb = _load_bootstrap()


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


PROC = {"pid": 4187, "name": "llama-server", "state": "running",
        "cpu_util_pct": 41.5, "ram_used_bytes": 3328599654,
        "threads": 9, "age_sec": 900}


class NodeCollectorTest(unittest.TestCase):
    """`process_facts()` reads /proc and emits only contract fields (§4.10)."""

    def setUp(self):
        tb._PROC_PREV.clear()

    def test_emits_only_contract_keys(self):
        tb.process_facts()  # baseline: establishes the CPU-tick deltas
        rows = tb.process_facts()
        self.assertIsInstance(rows, list)
        self.assertGreaterEqual(len(rows), 1, "a live host always runs something")
        for entry in rows:
            self.assertTrue(set(entry) <= set(F.PROCESS_FIELDS),
                            f"emitted key outside §4.10: {sorted(set(entry) - set(F.PROCESS_FIELDS))}")
            self.assertIn("pid", entry)
            self.assertIn("ram_used_bytes", entry)

    def test_no_c2_keys_by_construction(self):
        # The collector never reads argv/environ/exe, and the entry it builds
        # carries none of those names — the denial is structural, not a filter.
        rows = tb.process_facts() or []
        for entry in rows:
            for key in entry:
                self.assertFalse(governance.is_c2_field_name(key),
                                 f"{key} is a C2 field name (§4.3)")

    def test_bounded_top_n(self):
        # The bound is the owner's note of 2026-10-16 (top-10 by RSS per sample),
        # pinned here so a drift in either the bound or the doc fails the build.
        self.assertEqual(F.PROCESSES_MAX, 10)
        tb.process_facts()
        rows = tb.process_facts()
        self.assertLessEqual(len(rows), F.PROCESSES_MAX)

    def test_ranking_is_by_rss_then_cpu(self):
        tb._PROC_PREV.clear()
        tb.process_facts()
        self.assertGreaterEqual(len(tb._PROC_PREV), 1)
        rows = tb.process_facts() or []
        rams = [r["ram_used_bytes"] for r in rows]
        self.assertEqual(rams, sorted(rams, reverse=True),
                         "`ram_used_bytes` is the primary rank (§4.10)")
        # CPU is the tie-break *inside* an equal RSS, never the primary key.
        for _ram, group in itertools.groupby(rows, key=lambda r: r["ram_used_bytes"]):
            pcts = [r["cpu_util_pct"] for r in group if "cpu_util_pct" in r]
            self.assertEqual(pcts, sorted(pcts, reverse=True))

    def test_rank_key_is_rss_then_cpu_then_pid(self):
        # Synthetic tuples, so the total order is pinned deterministically rather
        # than by whichever processes this host happens to be running.
        rows = [(50.0, 1000, 7), (-1.0, 9000, 3), (2.0, 1000, 9), (80.0, 1000, 5)]
        self.assertEqual(
            sorted(rows, key=tb._rank_key),
            [(-1.0, 9000, 3), (80.0, 1000, 5), (50.0, 1000, 7), (2.0, 1000, 9)],
        )
        # A missing CPU delta (the -1.0 sentinel) sorts below a real 0.0 on equal
        # RSS instead of being treated as zero.
        self.assertEqual(sorted([(-1.0, 10, 1), (0.0, 10, 2)], key=tb._rank_key)[0][2], 2)

    def test_bounded_window_is_the_rss_heaviest(self):
        # The window must be RSS-optimal: no process left out may be heavier than
        # one kept. Checked against the full table (`/proc` read with the bound
        # lifted), so the assertion is about the ranking, not about this host.
        tb._PROC_PREV.clear()
        tb.process_facts()
        with mock.patch.object(tb, "PROCESSES_MAX", 10 ** 6):
            full = tb.process_facts() or []
        bounded = tb.process_facts() or []
        self.assertTrue(bounded)
        self.assertEqual(len(bounded), min(len(full), F.PROCESSES_MAX))
        kept = {entry["pid"] for entry in bounded}
        floor = min(entry["ram_used_bytes"] for entry in bounded)
        for entry in full:
            if entry["pid"] not in kept:
                self.assertLessEqual(
                    entry["ram_used_bytes"], floor,
                    f"pid {entry['pid']} has more RSS than a kept process")
        self.assertTrue(any("cpu_util_pct" in entry for entry in bounded))

    def test_first_sample_omits_cpu_rather_than_faking_zero(self):
        tb._PROC_PREV.clear()
        rows = tb.process_facts() or []
        self.assertTrue(rows)
        self.assertFalse([r for r in rows if "cpu_util_pct" in r],
                         "no CPU delta exists yet, so the field must be absent")

    def test_unreadable_proc_is_none_not_an_empty_list(self):
        original = tb.os.listdir

        def boom(path):
            if path == "/proc":
                raise OSError("no /proc")
            return original(path)

        tb.os.listdir = boom
        try:
            self.assertIsNone(tb.process_facts())
        finally:
            tb.os.listdir = original

    def test_task_name_is_cleaned_to_the_contract_pattern(self):
        # `stat` is `pid (comm) state ...` and comm may contain spaces/parens;
        # the name must come out printable-ASCII and <=15 chars.
        parsed = tb._comm_of("4187 (weird (name) here) S 1 4187 4187 0 -1")
        self.assertIsNotNone(parsed)
        self.assertLessEqual(len(parsed), 15)
        self.assertRegex(parsed, F.PROCESS_FIELDS["name"]["pattern"])
        self.assertIsNone(tb._comm_of("no parens at all"))

    def test_state_letters_map_onto_the_contract_enum(self):
        tb.process_facts()
        for entry in tb.process_facts() or []:
            if "state" in entry:
                self.assertIn(entry["state"], F.PROCESS_FIELDS["state"]["enum"])


class IngestAcceptsTest(unittest.TestCase):
    """A contract-valid sample carrying the new fields passes ingest."""

    def test_processes_accepted(self):
        clean, errors = validate_sample(_sample(processes=[PROC]))
        self.assertEqual(errors, [])
        self.assertEqual(clean["processes"][0]["name"], "llama-server")
        self.assertEqual(governance.scan_c2(clean), [])

    def test_processes_is_a_metric_field(self):
        _clean, errors = validate_sample({"seq": 1, "ts": "2026-10-04T07:49:00.000Z",
                                          "processes": [PROC]})
        self.assertEqual(errors, [])

    def test_operational_fields_on_instances_and_services(self):
        sample = _sample(
            lxd_instances=[{"vm_id": "01M4349RPXWDC52ZDAVMPCMJSH", "state": "running",
                            "state_age_sec": 812, "restart_count": 1}],
            services=[{"name": "llama-cpp", "status": "running", "state_age_sec": 900,
                       "restart_count": 2, "last_exit_code": 137,
                       "journal_errors": 1, "journal_warnings": 4}],
        )
        clean, errors = validate_sample(sample)
        self.assertEqual(errors, [])
        self.assertEqual(clean["services"][0]["last_exit_code"], 137)
        self.assertEqual(clean["services"][0]["journal_errors"], 1)
        self.assertEqual(governance.scan_c2(clean), [])

    def test_negative_exit_code_is_legal_systemd(self):
        sample = _sample(services=[{"name": "lxd", "status": "failed", "last_exit_code": -1}])
        _clean, errors = validate_sample(sample)
        self.assertEqual(errors, [])


class IngestRejectsTest(unittest.TestCase):
    """The C2 denial and the §4.10 bound survive the extension."""

    def test_cmdline_nested_in_a_process_entry_is_c2(self):
        for key in ("cmdline", "argv", "args", "env", "environ", "exe", "cwd",
                    "log_lines", "message"):
            entry = dict(PROC, **{key: "ps aux --some-flag"})
            self.assertTrue(governance.scan_c2(_sample(processes=[entry])),
                            f"{key} nested in processes[] must be a C2 hit")

    def test_c2_nested_in_a_service_entry_is_rejected(self):
        sample = _sample(services=[{"name": "llama-cpp", "status": "failed",
                                    "message": "i/o error on /var/lib/termini"}])
        self.assertTrue(governance.scan_c2(sample))

    def test_process_name_may_not_be_prose(self):
        for bad in ("a very long process name over fifteen", "proc\nname", "pröc"):
            _clean, errors = validate_sample(_sample(processes=[dict(PROC, name=bad)]))
            self.assertTrue(errors, f"name {bad!r} must not validate")

    def test_over_long_process_array_is_rejected_not_truncated(self):
        pids = [dict(PROC, pid=1000 + i) for i in range(F.PROCESSES_MAX + 1)]
        _clean, errors = validate_sample(_sample(processes=pids))
        self.assertTrue(any("exceeds the contract maximum" in e for e in errors), errors)

    def test_process_field_types_and_ranges(self):
        cases = [
            ("pid", 0), ("pid", "4187"), ("state", "sleepy"),
            ("cpu_util_pct", 101.0), ("threads", 0), ("age_sec", -1),
            ("ram_used_bytes", -1),
        ]
        for field, value in cases:
            _clean, errors = validate_sample(_sample(processes=[dict(PROC, **{field: value})]))
            self.assertTrue(errors, f"{field}={value!r} must not validate")

    def test_unknown_nested_key_is_stripped(self):
        clean, errors = validate_sample(_sample(processes=[dict(PROC, wat="x")]))
        self.assertEqual(errors, [])
        self.assertNotIn("wat", clean["processes"][0])

    def test_journal_counts_must_be_counts(self):
        _clean, errors = validate_sample(_sample(
            services=[{"name": "lxd", "status": "running", "journal_errors": "many"}]))
        self.assertTrue(errors)


class ContractMirrorTest(unittest.TestCase):
    """The new groups are registered in the allowlist and in governance.md."""

    def test_registered_as_c0(self):
        self.assertEqual(F.PROCESS_FIELDS["name"]["cclass"], "C0")
        self.assertEqual(F.SERVICE_FIELDS["journal_errors"]["cclass"], "C0")
        self.assertEqual(F.LXD_INSTANCE_FIELDS["restart_count"]["cclass"], "C0")

    def test_no_contract_field_name_tokenises_to_c2(self):
        names = set(F.PROCESS_FIELDS) | set(F.SERVICE_FIELDS) | set(F.LXD_INSTANCE_FIELDS)
        for name in names:
            self.assertFalse(governance.is_c2_field_name(name),
                             f"{name} is a legitimate field but reads as C2")

    def test_governance_doc_registers_the_groups(self):
        doc = (REPO / "docs" / "contracts" / "governance.md").read_text()
        self.assertIn("`processes[]`", doc)
        self.assertIn("journal_errors", doc)
        self.assertIn("Top-10 per sample by RSS", doc)

    def test_contract_doc_documents_the_new_sections(self):
        doc = (REPO / "docs" / "contracts" / "node-telemetry.md").read_text()
        self.assertIn("### 4.10", doc)
        self.assertIn("### 4.11", doc)
        # The doc must state the bound the code implements, in the same words:
        # top-10, RSS-first, with the CPU tie-break, and no new cadence.
        self.assertRegex(doc, r"top-10 per sample, ranked by\s+`ram_used_bytes`")
        self.assertIn("Recorded objection", doc)
        # §4.6's measured asymmetry is stated too, so the doc does not promise a
        # field a snap-LXD node can never fill.
        self.assertIn("`state_age_sec` is **structurally absent**", doc)


class MetricExpansionCollectorTest(unittest.TestCase):
    """The §4.12–§4.14 collectors, checked against the contract and the C2 rules.

    `tests/test_bootstrap_telemetry.py` pins the parsing (fixture, omission,
    bounds); this class pins the two boundaries that are about the *contract*
    rather than any one value: every key the collector can emit is a field the
    contract declares, and the collectors never read a C2 path — the conntrack
    table, a process `cmdline`, an environment or an `exe` link.
    """

    def test_every_collected_key_is_a_contract_field(self):
        host = tb.pressure_facts() or {}
        self.assertTrue(set(host) <= set(F.PRESSURE_FIELDS),
                        f"outside §4.12: {sorted(set(host) - set(F.PRESSURE_FIELDS))}")
        for group, allowed in (
            (tb.memory_pressure_scalars(), set(F.SAMPLE_FIELDS)),
            (tb.transport_scalars(), set(F.SAMPLE_FIELDS)),
            (tb.instance_cgroup_facts("demo-node-a"), set(F.LXD_INSTANCE_FIELDS)),
        ):
            self.assertTrue(set(group) <= allowed,
                            f"emitted key outside the contract: {sorted(set(group) - allowed)}")
        for row in tb.disk_facts():
            self.assertTrue(set(row) <= set(F.DISK_FIELDS),
                            f"outside §4.3/§4.13: {sorted(set(row) - set(F.DISK_FIELDS))}")
        for row in tb.net_facts({"controller_url": "http://127.0.0.1:9"}, "lo"):
            self.assertTrue(set(row) <= set(F.NET_FIELDS),
                            f"outside §4.4/§4.13: {sorted(set(row) - set(F.NET_FIELDS))}")

    def test_no_c2_field_name_is_emitted(self):
        keys = set(tb.memory_pressure_scalars()) | set(tb.transport_scalars())
        keys |= set(tb.instance_cgroup_facts("demo-node-a"))
        for row in tb.disk_facts():
            keys |= set(row)
        for key in keys:
            self.assertFalse(governance.is_c2_field_name(key),
                             f"{key} is a C2 field name (§4.3)")

    def test_the_collectors_never_read_a_c2_path(self):
        # A behavioural proof rather than a source grep: every path the new
        # collectors ask for is recorded, and the conntrack TABLE, any `cmdline`,
        # `environ`, `exe` or `cwd` must not be among them. `process_facts()` is
        # included so the whole collector surface is covered.
        reads: list = []
        original = tb._read_text

        def spy(path):
            reads.append(path)
            return original(path)

        tb._read_text = spy
        try:
            tb.pressure_facts()
            tb.memory_pressure_scalars()
            tb.transport_scalars()
            tb.disk_facts()
            tb.net_facts({"controller_url": "http://127.0.0.1:9"}, "lo")
            tb.instance_cgroup_facts("demo-node-a")
            tb.process_facts()
        finally:
            tb._read_text = original
        # Positive control: the spy really does see the reads that happen.
        self.assertIn("/proc/vmstat", reads)
        self.assertIn("/proc/pressure/memory", reads)
        for path in reads:
            stripped = path.replace("nf_conntrack_count", "").replace("nf_conntrack_max", "")
            self.assertNotIn("nf_conntrack", stripped,
                             "the conntrack TABLE is C2 and must never be read")
            for forbidden in ("cmdline", "environ", "/exe", "cwd", "user-data"):
                self.assertNotIn(forbidden, path)

    def test_a_collected_sample_passes_the_real_c2_scan(self):
        sample = _sample(
            pressure=tb.pressure_facts(),
            disk=tb.disk_facts(),
        )
        sample.update(tb.memory_pressure_scalars())
        sample.update(tb.transport_scalars())
        if sample["pressure"] is None:
            del sample["pressure"]
        clean, errors = validate_sample(sample)
        self.assertEqual(errors, [])
        self.assertEqual(governance.scan_c2(clean), [])


if __name__ == "__main__":
    unittest.main()
