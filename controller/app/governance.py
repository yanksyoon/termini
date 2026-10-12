"""Governance gate (controller-side re-verification) — governance.md §9 hook 1.

Re-runs, on every inbound telemetry batch, the checks the node-side redactor is
supposed to have performed, and rejects discrepancies:

- C2 signature scan (field-name denylist + PII regex signatures) -> fail-closed.
- C1 transform check (hostname/mac hashed, IP truncated) -> untransformed rejects.
- `assertRedactorVersion(min)` — reject stale redactor configs.
"""
from __future__ import annotations

import re
from typing import Any

from . import fields as F

# --- C1 transform patterns (mirror fields.py C1 specs) ---
_HOSTNAME_HASH = re.compile(r"^[0-9a-f]{16}$")
_MAC_HASH = re.compile(r"^[0-9a-f]{16}$")
_IPV4_TRANSFORMED = re.compile(r"^(?:\d{1,3}\.){3}0/24$")
_IPV6_TRANSFORMED = re.compile(r"^[0-9a-fA-F:]+/48$")

# --- C2 signatures (governance.md §5.1) ---
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)\+?[0-9]{10,15}(?!\d)")
_RAW_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Real IPv6 only: full 8-group form (7 colons) or compressed (contains "::").
# A time-of-day like "07:49:00" has neither and is correctly NOT matched.
_RAW_IPV6 = re.compile(
    r"\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b"
    r"|\b[0-9a-fA-F]{0,4}::[0-9a-fA-F:]*\b"
)
_RAW_MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_AWS_KEY = re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b")
_CRED = re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*\S+")
_CARD = re.compile(r"\b(?:\d[ -]?){13,16}\b")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
# Base64 blob: only unambiguous base64 (contains +, /, or =). Pure hex hashes
# (config_hash, C1 hashes) are legitimate and do not match.
_B64 = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")

_C1_FIELDS = {"hostname", "ip_local", "mac_address", "project_id"}

# Fields exempted from the C2 string scan. Every member MUST be structurally
# validated by the schema, because an exempted field is never signature-scanned for
# PII — so the exemption is only safe if the format makes prose impossible.
# `governance_chain_tip` is a ULID (validate.py) and `config_hash` a hex digest
# (fields.REDACTION_FIELDS). Both were previously weaker than that — "non-empty
# string" and a bare `str` — while being persisted, which made either one a storage
# path for unscanned customer content. The schema now enforces the formats the
# exemption assumes; do not add a member here without the same guarantee.
_SKIP_STRING_SCAN = _C1_FIELDS | {
    "ts", "call_id", "node_id", "vm_id", "governance_chain_tip", "config_hash",
}

# Field-name tokeniser for C2 matching (see is_c2_field_name). Splits on any
# non-alphanumeric run, so `app_logs` -> ["app", "logs"] and `appLogs` -> same.
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")


def check_c1_transforms(raw: Any) -> list[str]:
    """Return C1 transform violations (empty list = clean)."""
    violations: list[str] = []

    def walk(node: Any, path: list[str]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "ip_public":
                    violations.append("ip_public: denied by default (governance §4.2)")
                    continue
                if key == "hostname" and isinstance(value, str) and not _HOSTNAME_HASH.match(value):
                    violations.append("hostname: expected SHA-256 truncated hash (16 hex)")
                if key == "mac_address" and isinstance(value, str) and not _MAC_HASH.match(value):
                    violations.append("mac_address: expected SHA-256 hash (16 hex)")
                if key == "ip_local" and isinstance(value, str):
                    if not (_IPV4_TRANSFORMED.match(value) or _IPV6_TRANSFORMED.match(value)):
                        violations.append(
                            "ip_local: expected transformed IP (IPv4 .0/24 or IPv6 /48)"
                        )
                walk(value, path + [key])
        elif isinstance(node, list):
            for item in node:
                walk(item, path)

    walk(raw, [])
    return violations


def is_c2_field_name(key: Any) -> bool:
    """True if a field NAME denotes C2 content (governance.md §4.3).

    Matching is TOKEN-based, not exact. §4.3 names whole *classes* of content —
    "application log lines", "customer payloads", "prompts", "file contents" — so a
    field called `app_logs` or `request_body` is C2 exactly as `logs` / `body` are.

    This mattered: with exact matching, `app_logs` fell through to the
    deny-by-default stripper and was discarded **silently** (HTTP 202), so a node
    whose redactor was compromised never learned that C2 had been dropped, and the
    deck's claim that a C2 field "is rejected by the governance gate" was not true
    of that path. Token matching routes it to the same fail-closed 422 as `logs`.

    False-positive safety: no key on the C0/C1 allowlist tokenises to a C2 term
    (asserted in tests), so legitimate telemetry is unaffected.
    """
    if not isinstance(key, str):
        return False
    if key in F.C2_FIELD_NAMES:
        return True
    return any(tok in F.C2_FIELD_NAMES for tok in _tokens(key))


def _tokens(key: str) -> list[str]:
    """Split a field name into lowercase word tokens (snake_case + camelCase)."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", key)
    return [t.lower() for t in _NON_ALNUM.split(spaced) if t]


def scan_c2(raw: Any) -> list[str]:
    """Return C2 signature violations (empty list = clean). Fail-closed on any hit."""
    violations: list[str] = []

    def walk(node: Any, path: list[str]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if is_c2_field_name(key):
                    violations.append(f"{'.'.join(path + [key])}: C2 field name")
                    continue
                walk(value, path + [key])
        elif isinstance(node, list):
            for item in node:
                walk(item, path)
        elif isinstance(node, str):
            leaf = path[-1] if path else ""
            if leaf in _SKIP_STRING_SCAN:
                return  # structured field (timestamp/ULID/hash/C1) — not a PII vector
            if _EMAIL.search(node):
                violations.append(f"{'.'.join(path)}: email signature")
            if _PHONE.search(node):
                violations.append(f"{'.'.join(path)}: phone-number signature")
            if _RAW_IPV4.search(node) and not _IPV4_TRANSFORMED.match(node):
                violations.append(f"{'.'.join(path)}: raw IPv4 address")
            if _RAW_IPV6.search(node) and not _IPV6_TRANSFORMED.match(node):
                violations.append(f"{'.'.join(path)}: raw IPv6 address")
            if _RAW_MAC.search(node):
                violations.append(f"{'.'.join(path)}: raw MAC address")
            if _PRIVATE_KEY.search(node):
                violations.append(f"{'.'.join(path)}: private key material")
            if _AWS_KEY.search(node):
                violations.append(f"{'.'.join(path)}: AWS credential")
            if _CRED.search(node):
                violations.append(f"{'.'.join(path)}: credential pattern")
            if _CARD.search(node):
                violations.append(f"{'.'.join(path)}: card-number signature")
            if _UUID.search(node):
                violations.append(f"{'.'.join(path)}: UUID signature")
            if _B64.search(node):
                violations.append(f"{'.'.join(path)}: base64 blob")

    walk(raw, [])
    return violations


def assert_redactor_version(version: str, minimum: int) -> str | None:
    """Return an error string if the redactor config version is below `minimum`."""
    if not isinstance(version, str):
        return f"_redaction.version: expected string, got {type(version).__name__}"
    m = re.match(r"^redactor-config v(\d+)$", version)
    if not m:
        return f"_redaction.version: unparseable {version!r} (expected 'redactor-config vN')"
    if int(m.group(1)) < minimum:
        return f"_redaction.version {version!r} below controller minimum v{minimum}"
    return None
