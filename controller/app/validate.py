"""Telemetry schema enforcement — mirrors `app/fields.py` (and therefore
`docs/contracts/node-telemetry.md`).

Strict, fail-closed validation:
- Type mismatches, range/enum/pattern violations on a *present* field reject.
- Unknown top-level/nested fields are stripped (deny-by-default).
- `ts` and `seq` are required; at least one metric field is required.
"""
from __future__ import annotations

import datetime
import re
from typing import Any

from . import fields as F
from . import ulid

_RFC3339 = re.compile(F.RFC3339_PATTERN)
# C1 ip_local must arrive already transformed: IPv4 last octet -> ".0/24",
# or IPv6 truncated to its /48 prefix.
_IPV4_TRANSFORMED = re.compile(r"^(?:\d{1,3}\.){3}0/24$")
_IPV6_TRANSFORMED = re.compile(r"^[0-9a-fA-F:]+/48$")
# Untransformed (raw) IPv4/IPv6 — presence in a C1 field is a governance violation.
_RAW_IPV4 = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_RAW_IPV6 = re.compile(r"^[0-9a-fA-F:]+$")


def parse_rfc3339_epoch(ts: str) -> float:
    """Seconds since Unix epoch (UTC) for an RFC3339 string. Raises ValueError."""
    if not _RFC3339.match(ts):
        raise ValueError(f"not RFC3339: {ts!r}")
    norm = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    dt = datetime.datetime.fromisoformat(norm)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_float(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_str(v: Any) -> bool:
    return isinstance(v, str)


def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _check_scalar(name: str, spec: dict, value: Any) -> str | None:
    kind = spec["kind"]
    if kind == "int" and not _is_int(value):
        return f"{name}: expected integer, got {type(value).__name__}"
    if kind == "float" and not _is_float(value):
        return f"{name}: expected number, got {type(value).__name__}"
    if kind == "str" and not _is_str(value):
        return f"{name}: expected string, got {type(value).__name__}"
    if kind == "bool" and not _is_bool(value):
        return f"{name}: expected boolean, got {type(value).__name__}"

    if "min" in spec and value < spec["min"]:
        return f"{name}: {value} below minimum {spec['min']}"
    if "max" in spec and value > spec["max"]:
        return f"{name}: {value} above maximum {spec['max']}"
    if "enum" in spec and value not in spec["enum"]:
        return f"{name}: {value!r} not in {spec['enum']}"
    if kind == "str":
        # NOTE: C1 transform checks (hostname/mac hash, IP truncation) are
        # governance concerns and are enforced separately in governance.py;
        # here we only check structural type/format.
        if spec.get("rfc3339"):
            try:
                parse_rfc3339_epoch(value)
            except ValueError:
                return f"{name}: invalid RFC3339 timestamp {value!r}"
        if spec.get("ulid") and not ulid.is_valid_ulid(value):
            return f"{name}: {value!r} is not a valid ULID"
    return None


def _validate_object(name: str, raw: Any, field_specs: dict[str, dict]) -> tuple[dict, list[str]]:
    """Validate a nested object; strips unknown fields, returns (clean, errors)."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return raw, [f"{name}: expected object, got {type(raw).__name__}"]
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        spec = field_specs.get(key)
        if spec is None:
            continue  # unknown nested field -> strip (deny-by-default)
        err = _check_scalar(f"{name}.{key}", spec, value)
        if err:
            errors.append(err)
        else:
            clean[key] = value
    return clean, errors


def _validate_array(name: str, raw: Any, field_specs: dict[str, dict]) -> tuple[list, list[str]]:
    errors: list[str] = []
    if not isinstance(raw, list):
        return raw, [f"{name}: expected array, got {type(raw).__name__}"]
    clean_items: list[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"{name}[{i}]: expected object, got {type(item).__name__}")
            continue
        item_clean, item_errors = _validate_object(f"{name}[{i}]", item, field_specs)
        errors.extend(item_errors)
        clean_items.append(item_clean)
    return clean_items, errors


def validate_sample(raw: Any) -> tuple[dict, list[str]]:
    """Validate one Sample object. Returns (cleaned_sample, errors).

    `cleaned_sample` contains only known, valid fields (unknown stripped).
    """
    errors: list[str] = []
    if not isinstance(raw, dict):
        return raw, [f"sample: expected object, got {type(raw).__name__}"]

    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if key in F.SAMPLE_FIELDS:
            err = _check_scalar(key, F.SAMPLE_FIELDS[key], value)
            if err:
                errors.append(err)
            else:
                clean[key] = value
        elif key in F.SAMPLE_CONTAINERS:
            spec = F.SAMPLE_CONTAINERS[key]
            if spec["kind"] == "list":
                items, item_errors = _validate_array(key, value, spec["fields"])
                errors.extend(item_errors)
                if item_errors:
                    clean[key] = items  # keep valid elements; rejections counted
                else:
                    clean[key] = items
            else:
                obj, obj_errors = _validate_object(key, value, spec["fields"])
                errors.extend(obj_errors)
                if not obj_errors:
                    clean[key] = obj
        # else: unknown field -> strip (deny-by-default)

    if "ts" not in clean:
        errors.append("ts: required")
    if "seq" not in clean:
        errors.append("seq: required")

    if not (set(clean) & F.METRIC_FIELDS):
        errors.append("sample: at least one metric field is required")

    return clean, errors


def validate_envelope(raw: Any) -> tuple[dict, list[str]]:
    """Validate the batch envelope. Returns (cleaned_envelope, errors)."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return raw, [f"envelope: expected object, got {type(raw).__name__}"]

    for key in F.ENVELOPE_REQUIRED:
        if key not in raw:
            errors.append(f"envelope: missing required field '{key}'")

    clean: dict[str, Any] = {}

    red = raw.get("_redaction")
    if isinstance(red, dict):
        red_clean, red_errors = _validate_object("_redaction", red, F.REDACTION_FIELDS)
        errors.extend(red_errors)
        if not red_errors:
            clean["_redaction"] = red_clean
    else:
        errors.append("_redaction: expected object")

    call_id = raw.get("call_id")
    if call_id is not None:
        if not (isinstance(call_id, str) and ulid.is_valid_ulid(call_id)):
            errors.append("call_id: not a valid ULID")
        else:
            clean["call_id"] = call_id

    node_id = raw.get("node_id")
    if node_id is not None:
        if not (isinstance(node_id, str) and ulid.is_valid_ulid(node_id)):
            errors.append("node_id: not a valid ULID")
        else:
            clean["node_id"] = node_id

    sv = raw.get("schema_version")
    if sv is not None:
        if not (_is_int(sv) and sv == 1):
            errors.append("schema_version: must be 1")
        else:
            clean["schema_version"] = sv

    tip = raw.get("governance_chain_tip")
    if tip is not None:
        if not (isinstance(tip, str) and tip):
            errors.append("governance_chain_tip: expected non-empty string")
        else:
            clean["governance_chain_tip"] = tip

    samples = raw.get("samples")
    if samples is not None:
        if not isinstance(samples, list):
            errors.append("samples: expected array")
        elif not (1 <= len(samples) <= 200):
            errors.append("samples: must contain 1..200 samples")
        else:
            clean_samples: list[dict] = []
            for i, s in enumerate(samples):
                s_clean, s_errors = validate_sample(s)
                if s_errors:
                    errors.append(f"samples[{i}]: {'; '.join(s_errors)}")
                clean_samples.append(s_clean)
            clean["samples"] = clean_samples

    return clean, errors
