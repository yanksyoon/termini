"""Deterministic clamp validator — the security boundary between LLM output
and LXD.

This module is **pure and LLM-free**: it contains no network, no model call,
no subprocess, and no I/O. It imports only :mod:`termini.intent.types`. The
operator/LXD layer MUST only ever consume :func:`clamp_validate` output
(:class:`~termini.intent.types.ValidationResult`); the raw LLM output must
never reach LXD.

Validation policy (task t_26f5ba13):
  * every numeric field is coerced to int, floored to its positive bound, then
    clamped to ``min(requested, host_max)``;
  * unknown component types, malformed components, missing required fields,
    and over-long component lists are REJECTED (all-or-nothing);
  * ``passthrough_gpu: true`` on ANY class is a structural REJECTION with reason
    ``gpu_passthrough_not_in_demo`` (controller-api.md §5.1/§5.3, day-2 only);
  * every alteration is reported as a ``ClampDecision`` (requested vs applied).

Full node-resolution / GPU / capacity-accounting guardrail logic
(controller-api.md §5.2 steps 5–8) lives in the wider controller service, not
here; this module is the deterministic numeric clamp those steps build on.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from .types import (
    ABSOLUTE_CAPS,
    FLOORS,
    KNOWN_TYPES,
    MAX_COMPONENTS,
    NUMERIC_FIELDS,
    REASON_ABOVE_ABSOLUTE_MAX,
    REASON_ABOVE_HOST_MAX,
    REASON_BELOW_MINIMUM,
    REASON_DEFAULTED,
    REASON_EMPTY,
    REASON_EXTRACTION_FAILED,
    REASON_GPU_PASSTHROUGH_NOT_IN_DEMO,
    REASON_MALFORMED,
    REASON_MISSING_COUNT,
    REASON_MISSING_TYPE,
    REASON_NON_INTEGER,
    REASON_TOO_MANY,
    REASON_UNKNOWN_TYPE,
    ROLE_FLOORS,
    AllocationSpec,
    ClampDecision,
    HostCaps,
    Rejection,
    ValidationResult,
)


def _coerce_int(value: Any) -> Optional[int]:
    """Coerce a JSON scalar to an int, or return None when not coercible.

    bool is explicitly rejected (True/False are not valid counts or sizes).
    Floats are truncated toward zero; integral strings are parsed.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None
        try:
            return int(text)
        except ValueError:
            try:
                return int(float(text))
            except (ValueError, OverflowError):
                return None
    return None


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _reject(result: ValidationResult, index: Optional[int], reason: str, detail: str = "") -> None:
    result.rejections.append(Rejection(index=index, reason=reason, detail=detail))


def _clamp_numeric(
    result: ValidationResult,
    index: int,
    field: str,
    raw: Any,
    floor: int,
    ceiling: int,
) -> Optional[int]:
    """Coerce, floor, and cap one numeric field. Returns the final int.

    Returns ``None`` (and records a ``non_integer`` rejection) when the value
    is not coercible. Records a ``ClampDecision`` for every alteration
    (coercion, flooring, capping).
    """
    coerced = _coerce_int(raw)
    if coerced is None:
        _reject(result, index, REASON_NON_INTEGER, f"{field}={raw!r} is not an integer")
        return None

    applied = coerced
    if applied < floor:
        result.decisions.append(
            ClampDecision(index, field, raw, floor, REASON_BELOW_MINIMUM)
        )
        applied = floor
    if applied > ceiling:
        reason = (
            REASON_ABOVE_ABSOLUTE_MAX
            if ceiling == ABSOLUTE_CAPS[field]
            else REASON_ABOVE_HOST_MAX
        )
        result.decisions.append(ClampDecision(index, field, raw, ceiling, reason))
        applied = ceiling
    return applied


def clamp_validate(components: Any, host_caps: Any) -> ValidationResult:
    """Validate and clamp raw LLM output into an LXD-safe spec.

    Args:
        components: raw extractor output — a list of component dicts (or an
            ``{"allocations": [...]}`` / ``{"components": [...]}`` envelope).
        host_caps: host-capacity table (dict or :class:`HostCaps`) with
            per-role maxima for ``vcpus``/``ram_gb``/``disk_gb``.

    Returns:
        :class:`ValidationResult`. On any rejection, ``allocations`` is empty
        and ``outcome == "rejected"`` (all-or-nothing).
    """
    result = ValidationResult()

    try:
        caps = HostCaps.from_dict(host_caps)
    except (ValueError, TypeError) as exc:
        _reject(result, None, REASON_MALFORMED, f"invalid host_caps: {exc}")
        result.outcome = "rejected"
        return result

    # Normalize the input envelope.
    if isinstance(components, dict):
        if "allocations" in components:
            components = components["allocations"]
        elif "components" in components:
            components = components["components"]
        else:
            _reject(result, None, REASON_MALFORMED, "envelope missing 'allocations'")
            result.outcome = "rejected"
            return result

    if not isinstance(components, list):
        _reject(result, None, REASON_MALFORMED, f"expected a list, got {type(components).__name__}")
        result.outcome = "rejected"
        return result

    if len(components) == 0:
        _reject(result, None, REASON_EMPTY, "no components supplied")
        result.outcome = "rejected"
        return result

    if len(components) > MAX_COMPONENTS:
        _reject(
            result,
            None,
            REASON_TOO_MANY,
            f"{len(components)} components exceeds max {MAX_COMPONENTS}",
        )
        result.outcome = "rejected"
        return result

    accepted: List[AllocationSpec] = []

    for index, component in enumerate(components):
        if not isinstance(component, dict):
            _reject(result, index, REASON_MALFORMED, f"component is {type(component).__name__}")
            continue

        comp_type = component.get("type")
        if comp_type is None:
            _reject(result, index, REASON_MISSING_TYPE, "component missing 'type'")
            continue
        if comp_type not in KNOWN_TYPES:
            _reject(result, index, REASON_UNKNOWN_TYPE, f"unknown type {comp_type!r}")
            continue

        # Structural rejection (controller-api.md §5.1/§5.3): GPU passthrough is
        # day-2 only, so `passthrough_gpu: true` on ANY class rejects the request
        # (reason gpu_passthrough_not_in_demo). Never clamped.
        if _coerce_bool(component.get("passthrough_gpu"), default=False):
            _reject(
                result,
                index,
                REASON_GPU_PASSTHROUGH_NOT_IN_DEMO,
                f"{comp_type} passthrough_gpu=true is day-2 only",
            )
            continue

        caps_for_role = caps.for_role(comp_type)
        role_floor = ROLE_FLOORS[comp_type]

        # count: floor 1, cap at the absolute structural cap.
        count_raw = component.get("count")
        if count_raw is None:
            _reject(result, index, REASON_MISSING_COUNT, "component missing required 'count'")
            continue
        count = _clamp_numeric(
            result, index, "count", count_raw, FLOORS["count"], ABSOLUTE_CAPS["count"]
        )
        if count is None:
            continue

        # vcpus / ram_gb / disk_gb: floor, then clamp to host max.
        host_max = {
            "vcpus": caps_for_role.vcpus,
            "ram_gb": caps_for_role.ram_gb,
            "disk_gb": caps_for_role.disk_gb,
        }

        values: Dict[str, int] = {"count": count}
        failed = False
        for field in ("vcpus", "ram_gb", "disk_gb"):
            raw = component.get(field)
            if raw is None:
                default = role_floor[field]
                result.decisions.append(
                    ClampDecision(index, field, None, default, REASON_DEFAULTED)
                )
                values[field] = default
                continue
            applied = _clamp_numeric(
                result, index, field, raw, FLOORS[field], host_max[field]
            )
            if applied is None:
                failed = True
                break
            values[field] = applied
        if failed:
            continue

        # passthrough_gpu was already rejected above when true; only false survives.
        target_node = component.get("target_node")
        if not isinstance(target_node, str) or target_node == "":
            target_node = "auto"

        accepted.append(
            AllocationSpec(
                type=comp_type,
                count=values["count"],
                vcpus=values["vcpus"],
                ram_gb=values["ram_gb"],
                disk_gb=values["disk_gb"],
                passthrough_gpu=False,
                target_node=target_node,
            )
        )

    if result.rejections:
        # All-or-nothing: nothing proceeds to LXD if any component was rejected.
        result.allocations = []
        result.outcome = "rejected"
        return result

    result.allocations = accepted
    result.outcome = "clamped" if result.decisions else "accepted"
    return result
