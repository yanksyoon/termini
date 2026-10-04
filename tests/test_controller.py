"""Unit + integration tests for the Termini Controller core.

Discovered by CI via `python3 -m unittest discover -s tests`. The integration
test boots the real server (subprocess) and runs the full acceptance suite.

Run locally:  python3 -m unittest discover -s tests -v   (from repo root)
"""
from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER = REPO / "controller"
sys.path.insert(0, str(CONTROLLER))
sys.path.insert(0, str(CONTROLLER / "scripts"))

from app import fields, governance, registration, role, ulid, validate  # noqa: E402

L4_24GB = 24 * 1024 ** 3


def valid_sample(**overrides) -> dict:
    s = {
        "seq": 1000,
        "ts": "2026-10-04T07:49:00.000Z",
        "cpu_util_pct": 31.2,
        "ram_total_bytes": 34359738368,
        "ram_used_bytes": 19756849561,
        "hostname": "5f2a9c1e7b4d4a1f",
        "ip_local": "10.0.0.0/24",
        "mac_address": "9c1e7b4d4a1f5f2a",
        "project_id": ulid.new_ulid(),
        "disk": [{"path": "/dev/sda", "total_bytes": 274877906944, "free_bytes": 100,
                  "write_latency_ms": 3.0, "mount_ro": False}],
        "net": [{"iface": "eth0", "rx_bytes": 123, "tx_bytes": 11, "latency_ms": 0.8}],
    }
    s.update(overrides)
    return s


def valid_envelope(node_id: str, samples: list[dict]) -> dict:
    return {
        "_redaction": {"version": "redactor-config v3", "config_hash": "3f2a1b", "result": "ok"},
        "call_id": ulid.new_ulid(),
        "node_id": node_id,
        "schema_version": 1,
        "governance_chain_tip": ulid.new_ulid(),
        "samples": samples,
    }


class UlidTest(unittest.TestCase):
    def test_generates_26_chars_valid_crockford(self):
        for _ in range(200):
            u = ulid.new_ulid()
            self.assertTrue(ulid.is_valid_ulid(u), u)
            self.assertEqual(len(u), 26)

    def test_rejects_bad(self):
        self.assertFalse(ulid.is_valid_ulid("01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C0D"))  # 30 chars
        self.assertFalse(ulid.is_valid_ulid("01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C"))    # short
        self.assertFalse(ulid.is_valid_ulid("0I4KZ9Q2R3T5V7W9X1Y3Z5A7B9CDEF"))  # contains I
        self.assertFalse(ulid.is_valid_ulid(None))


class RoleTest(unittest.TestCase):
    def test_no_gpu_is_compute(self):
        self.assertEqual(role.assign_role(0, 0, 1), "compute")

    def test_gpu_below_threshold_is_compute(self):
        # 11 GiB < 12 GiB threshold
        self.assertEqual(role.assign_role(11 * 1024 ** 3, 0, 1), "compute")

    def test_gpu_at_threshold_is_inference(self):
        self.assertEqual(role.assign_role(12 * 1024 ** 3, 0, 1), "inference")

    def test_l4_is_inference(self):
        self.assertEqual(role.assign_role(L4_24GB, 0, 1), "inference")

    def test_cap_forces_compute(self):
        self.assertEqual(role.assign_role(L4_24GB, 1, 1), "compute")

    def test_cap_raised_allows_second(self):
        self.assertEqual(role.assign_role(L4_24GB, 1, 2), "inference")


class ValidationTest(unittest.TestCase):
    def test_valid_envelope_passes(self):
        env = valid_envelope(ulid.new_ulid(), [valid_sample()])
        clean, errs = validate.validate_envelope(env)
        self.assertEqual(errs, [])
        self.assertEqual(clean["samples"][0]["cpu_util_pct"], 31.2)

    def test_unknown_field_stripped(self):
        env = valid_envelope(ulid.new_ulid(), [valid_sample(extra_junk="x")])
        clean, errs = validate.validate_envelope(env)
        self.assertEqual(errs, [])
        self.assertNotIn("extra_junk", clean["samples"][0])

    def test_out_of_range_rejected(self):
        env = valid_envelope(ulid.new_ulid(), [valid_sample(cpu_util_pct=250)])
        _, errs = validate.validate_envelope(env)
        self.assertTrue(any("cpu_util_pct" in e for e in errs), errs)

    def test_wrong_type_rejected(self):
        env = valid_envelope(ulid.new_ulid(), [valid_sample(cpu_util_pct="abc")])
        _, errs = validate.validate_envelope(env)
        self.assertTrue(errs)

    def test_missing_ts_rejected(self):
        s = valid_sample()
        s.pop("ts")
        _, errs = validate.validate_envelope(valid_envelope(ulid.new_ulid(), [s]))
        self.assertTrue(any("ts" in e for e in errs), errs)

    def test_missing_metric_rejected(self):
        s = {"seq": 1, "ts": "2026-10-04T07:49:00.000Z"}
        _, errs = validate.validate_envelope(valid_envelope(ulid.new_ulid(), [s]))
        self.assertTrue(any("metric" in e for e in errs), errs)

    def test_negative_ram_rejected(self):
        env = valid_envelope(ulid.new_ulid(), [valid_sample(ram_used_bytes=-5)])
        _, errs = validate.validate_envelope(env)
        self.assertTrue(any("ram_used_bytes" in e for e in errs), errs)


class GovernanceTest(unittest.TestCase):
    def test_c2_field_name_detected(self):
        env = valid_envelope(ulid.new_ulid(), [valid_sample(prompt="hello")])
        self.assertTrue(governance.scan_c2(env))

    def test_raw_email_detected(self):
        env = valid_envelope(ulid.new_ulid(), [valid_sample(kernel_version="a@b.com")])
        self.assertTrue(governance.scan_c2(env))

    def test_raw_ipv4_in_free_field_detected(self):
        env = valid_envelope(ulid.new_ulid(), [valid_sample(kernel_version="192.168.1.10")])
        self.assertTrue(governance.scan_c2(env))

    def test_timestamp_not_false_positive(self):
        env = valid_envelope(ulid.new_ulid(), [valid_sample()])
        self.assertEqual(governance.scan_c2(env), [])

    def test_untransformed_ip_local_detected(self):
        env = valid_envelope(ulid.new_ulid(), [valid_sample(ip_local="10.0.0.12")])
        self.assertTrue(governance.check_c1_transforms(env))

    def test_ip_public_denied(self):
        env = valid_envelope(ulid.new_ulid(), [valid_sample(ip_public="1.2.3.4")])
        self.assertTrue(governance.check_c1_transforms(env))

    def test_redactor_version(self):
        self.assertIsNone(governance.assert_redactor_version("redactor-config v3", 3))
        self.assertIsNone(governance.assert_redactor_version("redactor-config v5", 3))
        self.assertIsNotNone(governance.assert_redactor_version("redactor-config v2", 3))
        self.assertIsNotNone(governance.assert_redactor_version("garbage", 3))


class RegistrationTest(unittest.TestCase):
    def test_canonical_parse(self):
        reg = {
            "schema_version": "1.0",
            "machine": {
                "machine_id": "m-c", "hostname": "node-c",
                "cpu": {"model": "x", "sockets": 1, "cores": 8, "threads": 16},
                "memory": {"total_bytes": 68719476736},
                "gpus": [{"vendor": "nvidia", "model": "NVIDIA L4", "vram_bytes": L4_24GB}],
            },
        }
        n = registration.parse_registration(reg)
        self.assertEqual(n["gpu_vram_bytes"], L4_24GB)
        self.assertEqual(n["cpu_cores"], 16)

    def test_legacy_parse(self):
        reg = {
            "registration_id": "termini-node-a",
            "hardware": {
                "cpu": {"logical_cores": 16},
                "ram_gb": 32,
                "gpu": [{"model": "NVIDIA L4", "vram_gb": 24, "count": 1}],
            },
        }
        n = registration.parse_registration(reg)
        self.assertEqual(n["gpu_vram_bytes"], 24 * 1024 ** 3)
        self.assertEqual(n["shape"], "legacy")

    def test_missing_machine_and_hardware_rejected(self):
        from app import errors
        with self.assertRaises(errors.ApiError):
            registration.parse_registration({"foo": "bar"})


class IntegrationTest(unittest.TestCase):
    def test_full_acceptance_suite(self):
        import acceptance
        self.assertEqual(acceptance.main(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
