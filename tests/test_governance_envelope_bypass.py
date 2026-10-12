"""Envelope fields exempted from the C2 scan must be structurally VALIDATED.

Regression cover for a real bypass found by adversarially re-verifying the
governance claim. `governance._SKIP_STRING_SCAN` exempts certain fields from the C2
signature scan, on the stated premise that they are "structurally-validated,
non-smuggling fields … never a PII vector". For two of them that premise was false:

    `_redaction.config_hash`   was a bare `str` in the schema — no constraint at all
    `governance_chain_tip`     was validated only as "non-empty string"

Both are PERSISTED (`telemetry_batches.redactor_config_hash`,
`telemetry_batches.governance_chain_tip`, plus `payload_json`). So an arbitrary
string — including an email address or a card number — placed in either field was
never scanned and was stored verbatim. That falsifies the strong form of the C2
claim ("rejected before any byte is stored") for that path.

The fix makes the schema enforce the formats the exemption always assumed: the chain
tip must be a ULID, the config hash a hex digest. Prose is then impossible by format,
so skipping the signature scan is legitimate.

THE INVARIANT THIS FILE EXISTS TO PROTECT: no member of `_SKIP_STRING_SCAN` may lack
a schema constraint. An exemption without a structural guarantee is a storage path.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER = REPO / "controller"
sys.path.insert(0, str(CONTROLLER))
sys.path.insert(0, str(REPO))

from app import fields as F          # noqa: E402
from app import governance as G      # noqa: E402
from app import validate as V        # noqa: E402

ULID = "01M4QC1JGXBXAYFQKMQ6MH6E00"
PII = "CUSTOMER-SECRET: contact jane.doe@acme-corp.example card 4111111111111111"


def _envelope(**overrides) -> dict:
    base = {
        "_redaction": {"version": "redactor-config v3",
                       "config_hash": "3f2a1b", "result": "ok"},
        "call_id": ULID,
        "node_id": ULID,
        "schema_version": 1,
        "governance_chain_tip": ULID,
        "samples": [],
    }
    base.update(overrides)
    return base


def _errors(env: dict) -> list[str]:
    _clean, errors = V.validate_envelope(env)
    return errors


class ConfigHashIsConstrained(unittest.TestCase):
    def test_hex_digest_accepted(self):
        for good in ("3f2a1b", "deadbeef", "a" * 128):
            with self.subTest(good=good):
                self.assertEqual(
                    [e for e in _errors(_envelope(
                        _redaction={"version": "redactor-config v3",
                                    "config_hash": good, "result": "ok"}))
                     if "config_hash" in e],
                    [], f"legitimate hex hash {good!r} must be accepted")

    def test_prose_rejected(self):
        """The bypass: PII in config_hash was previously stored unscanned."""
        errs = _errors(_envelope(
            _redaction={"version": "redactor-config v3",
                        "config_hash": PII, "result": "ok"}))
        self.assertTrue([e for e in errs if "config_hash" in e],
                        "prose in config_hash must be rejected by the schema")

    def test_email_rejected(self):
        errs = _errors(_envelope(
            _redaction={"version": "redactor-config v3",
                        "config_hash": "jane.doe@acme-corp.example", "result": "ok"}))
        self.assertTrue([e for e in errs if "config_hash" in e])


class ChainTipIsAULID(unittest.TestCase):
    def test_valid_ulid_accepted(self):
        self.assertEqual([e for e in _errors(_envelope()) if "chain_tip" in e], [])

    def test_prose_rejected(self):
        errs = _errors(_envelope(governance_chain_tip=PII))
        self.assertTrue([e for e in errs if "chain_tip" in e],
                        "prose in governance_chain_tip must be rejected by the schema")

    def test_empty_string_rejected(self):
        errs = _errors(_envelope(governance_chain_tip=""))
        self.assertTrue([e for e in errs if "chain_tip" in e])


class SkipScanMembersMustBeStructurallyValidated(unittest.TestCase):
    """The invariant: an exempted field with no constraint is a storage path.

    Scope note: several members are constrained *in code* rather than by a schema
    `pattern`/`enum` — `ts` by an RFC3339 match, `ip_local` by the C1 transform check,
    the ULID identifiers by `ulid.is_valid_ulid`. So this class does two things it can
    actually prove: it PINS the membership, so widening the exemption is a deliberate
    reviewed change rather than a quiet one; and the case-specific classes above prove
    the two fields that were unconstrained now reject prose.
    """

    # The exemption as of this fix. Adding a member here is a security-relevant change:
    # the new field is no longer signature-scanned for PII, so it must be format-
    # constrained first.
    EXPECTED = {
        "ts", "call_id", "node_id", "vm_id", "governance_chain_tip", "config_hash",
        "hostname", "ip_local", "mac_address", "project_id",
    }

    def test_skip_scan_membership_is_pinned(self):
        self.assertEqual(set(G._SKIP_STRING_SCAN), self.EXPECTED,
                         "the C2-scan exemption changed — review that any new member "
                         "is format-constrained before accepting it")

    def test_config_hash_spec_is_constrained(self):
        spec = F.REDACTION_FIELDS["config_hash"]
        self.assertIn("pattern", spec,
                      "config_hash is exempt from the C2 scan, so it MUST carry a "
                      "format constraint (it was a bare str while being persisted)")


class PiiStillScanRejectedWhereScanned(unittest.TestCase):
    """The signature scanner itself must still be live (guard against a dead check)."""

    def test_pii_in_an_unexempted_field_is_flagged(self):
        violations = G.scan_c2({"samples": [{"note_free": "jane.doe@acme-corp.example"}]})
        self.assertTrue(violations, "C2 scanner must still flag an email in a scanned field")


if __name__ == "__main__":
    unittest.main()
