"""C2 field-NAME detection must be token-based, not exact (governance.md §4.3).

Regression cover for a real gap found by red-teaming the deployed controller:

    a telemetry batch carrying `app_logs` (an application log line, explicitly C2
    per §4.3) was answered **202 Accepted** — the field fell through the exact-match
    C2 deny-list into the deny-by-default stripper and was discarded silently. The
    node got no signal that C2 had been dropped, and the claim "a C2 field is
    rejected by the governance gate" was false for that path.

The fix tokenises field names, so `app_logs` is C2 exactly as `logs` is. The
central safety property is the OPPOSITE direction: no legitimate C0/C1 field may be
caught, or valid telemetry would start failing closed.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER = REPO / "controller"
sys.path.insert(0, str(CONTROLLER))
sys.path.insert(0, str(REPO))

from app import fields as F  # noqa: E402
from app import governance as G  # noqa: E402

# Every field name a conforming node may legitimately send.
def _legit_field_names() -> set[str]:
    names = set(F.ALL_SAMPLE_KEYS)
    names |= set(F.ENVELOPE_REQUIRED) | set(F.ENVELOPE_OPTIONAL)
    names |= set(F.REDACTION_FIELDS)
    for spec in F.SAMPLE_CONTAINERS.values():
        names |= set(spec.get("fields") or {})
    return names


class FalsePositiveSafetyTest(unittest.TestCase):
    """The dangerous direction: breaking valid telemetry."""

    def test_no_allowlisted_field_is_treated_as_c2(self):
        offenders = sorted(n for n in _legit_field_names() if G.is_c2_field_name(n))
        self.assertEqual(
            offenders, [],
            f"legitimate field(s) classified as C2 — valid telemetry would now "
            f"fail closed on the ingest path: {offenders}",
        )

    def test_clean_batch_produces_no_c2_violations(self):
        sample = {
            "ts": "2026-10-11T19:00:00.000Z", "seq": 1, "uptime_sec": 60,
            "cpu_util_pct": 40.0, "cpu_core_count": 2,
            "ram_total_bytes": 1073741824, "ram_used_bytes": 268435456,
            "swap_used_bytes": 0, "load_avg_1m": 1.0,
            "os_name": "Ubuntu", "os_version": "24.04",
            "kernel_version": "6.8.0-142-generic", "lxd_cluster_role": "member",
            "hostname": "061bf633d3ad535a", "ip_local": "10.14.100.0/24",
            "mac_address": "3874d7396d87b2e7", "project_id": "01J0DEM0WARM000000000000AB",
            "disk": [{"path": "/dev/sda", "total_bytes": 10737418240,
                      "free_bytes": 5368709120, "write_latency_ms": 3.0, "mount_ro": False}],
            "net": [{"iface": "eth0", "rx_bytes": 1000, "tx_bytes": 500, "latency_ms": 0.8}],
            "services": [{"name": "lxd", "status": "running"}],
            "lxd_instances": [],
        }
        env = {"_redaction": {"version": "redactor-config v3", "config_hash": "3f2a1b",
                              "result": "ok"},
               "call_id": "01J0DEM0WARM000000000000AB", "node_id": "01J0DEM0WARM000000000000AB",
               "schema_version": 1, "governance_chain_tip": "01J0DEM0WARM000000000000AB",
               "samples": [sample]}
        self.assertEqual(G.scan_c2(env), [], "a conforming batch must scan clean")


class C2NameDetectionTest(unittest.TestCase):

    def test_the_regression_case(self):
        # The exact field that slipped through as 202 on the deployed controller.
        self.assertTrue(G.is_c2_field_name("app_logs"))

    def test_field_name_classes_match_their_term(self):
        for key in ("logs", "log", "log_line", "syslog", "app_logs", "error_log",
                    "requestLog", "conversation", "conversation_log", "transcript",
                    "document", "doc", "doc_content", "customer", "customer_notes",
                    "certificate", "cert_pem", "prompt", "prompt_text",
                    "request_body", "cmdline", "ssh_key", "private_key", "secret",
                    "credential", "payload", "file_contents"):
            with self.subTest(key=key):
                self.assertTrue(G.is_c2_field_name(key), f"{key!r} should be C2")

    def test_unrelated_names_are_not_c2(self):
        for key in ("cpu_util_pct", "ram_used_bytes", "disk", "net", "gpu",
                    "services", "lxd_instances", "hostname", "ip_local", "seq",
                    "ts", "boot", "image_version", "uptime_sec", "iface",
                    "write_latency_ms", "vm_id", "vram_total_bytes"):
            with self.subTest(key=key):
                self.assertFalse(G.is_c2_field_name(key), f"{key!r} must not be C2")

    def test_scan_c2_flags_c2_field_names_and_reports_the_path(self):
        raw = {"samples": [{"seq": 1, "app_logs": ["2026-10-11 INFO worker ready"]}]}
        v = G.scan_c2(raw)
        self.assertTrue(v, "app_logs must be flagged")
        self.assertIn("C2 field name", v[0])
        self.assertIn("app_logs", v[0])

    def test_scan_c2_is_fail_closed_on_nested_c2(self):
        raw = {"samples": [{"seq": 1, "lxd_instances": [{"customer_notes": "x"}]}]}
        self.assertTrue(G.scan_c2(raw), "nested C2 must still be caught")

    def test_non_string_keys_do_not_crash(self):
        self.assertFalse(G.is_c2_field_name(None))
        self.assertFalse(G.is_c2_field_name(7))


class StripVisibilityTest(unittest.TestCase):
    """Deny-by-default stripping is deliberate; doing it SILENTLY is what hid the bug."""

    def test_stripped_keys_are_identified(self):
        from app import server as S
        raw = [{"seq": 1, "ts": "2026-10-11T19:00:00.000Z", "cpu_util_pct": 1.0,
                "app_logs": ["x"], "zzz_unknown": 1}]
        clean = [{"seq": 1, "ts": "2026-10-11T19:00:00.000Z", "cpu_util_pct": 1.0}]
        self.assertEqual(S._stripped_sample_keys(raw, clean),
                         {"app_logs", "zzz_unknown"})

    def test_nothing_reported_when_nothing_stripped(self):
        from app import server as S
        s = [{"seq": 1, "ts": "2026-10-11T19:00:00.000Z"}]
        self.assertEqual(S._stripped_sample_keys(s, s), set())

    def test_tolerates_malformed_input(self):
        from app import server as S
        self.assertEqual(S._stripped_sample_keys(None, []), set())
        self.assertEqual(S._stripped_sample_keys([1, 2], [{}, {}]), set())


if __name__ == "__main__":
    unittest.main()
