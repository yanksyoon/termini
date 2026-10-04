"""Typed data structures for resource-intent extraction and clamp validation.

These are the ONLY structures the operator/LXD layer may consume. The raw LLM
output (an untyped list of dicts) never leaves this package except through
``validator.clamp_validate``, whose output is a fully validated
:class:`ValidationResult` of freshly-constructed :class:`AllocationSpec`
objects.

Field names and enum values follow ``docs/contracts/vm-allocation.schema.json``
and ``docs/contracts/controller-api.md`` §5. All counts/cores/sizes are
integers; no fractional vCPU or RAM is allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Frozen bounds (controller-api.md §5.1 + vm-allocation.schema.json)
# ---------------------------------------------------------------------------

# Absolute structural caps: never exceedable, regardless of host capacity.
ABSOLUTE_CAPS: Dict[str, int] = {
    "vcpus": 128,
    "ram_gb": 512,
    "disk_gb": 4096,
    "count": 64,
}

# Structural floors (schema minimums). Positive/floor bounds.
FLOORS: Dict[str, int] = {
    "count": 1,
    "vcpus": 1,
    "ram_gb": 1,
    "disk_gb": 4,
}

# The two VM classes the demo wires (vm-allocation.schema.json §vm_type).
KNOWN_TYPES: Tuple[str, ...] = ("agent_vm", "inference_vm")

# allocations maxItems (schema §allocation_request).
MAX_COMPONENTS: int = 64

# Preferred per-role floors used only when the LLM omits a numeric field, so
# the validator remains total over malformed input. agent_vm / inference_vm
# floor values mirror controller-api.md §5.1 preferred-range minima.
ROLE_FLOORS: Dict[str, Dict[str, int]] = {
    "agent_vm": {"vcpus": 1, "ram_gb": 1, "disk_gb": 4},
    "inference_vm": {"vcpus": 4, "ram_gb": 8, "disk_gb": 32},
}

# Demo defaults (controller-api.md §5.1 preferred-range maxima). Production
# injects real per-host capacity; these are the frozen demo ceilings.
DEFAULT_HOST_CAPS: Dict[str, Dict[str, int]] = {
    "agent_vm": {"vcpus": 8, "ram_gb": 16, "disk_gb": 64},
    "inference_vm": {"vcpus": 32, "ram_gb": 64, "disk_gb": 512},
}

# Numeric fields subject to integer coercion + floor + host-max clamp.
NUMERIC_FIELDS: Tuple[str, ...] = ("count", "vcpus", "ram_gb", "disk_gb")

# Reason codes for clamp decisions (subset of the schema's clamped_field enum).
REASON_BELOW_MINIMUM = "below_minimum"
REASON_ABOVE_HOST_MAX = "above_host_max"
REASON_ABOVE_ABSOLUTE_MAX = "above_absolute_maximum"
REASON_DEFAULTED = "defaulted"
REASON_NON_INTEGER = "non_integer"
REASON_UNKNOWN_TYPE = "unknown_component_type"
REASON_MISSING_TYPE = "missing_component_type"
REASON_MISSING_COUNT = "missing_required_field"
REASON_MALFORMED = "malformed_component"
REASON_TOO_MANY = "max_component_count_exceeded"
REASON_EMPTY = "empty_components"
REASON_EXTRACTION_FAILED = "extraction_failed"


# ---------------------------------------------------------------------------
# Host capacity table
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RoleCaps:
    """Per-role maxima for a single VM class. Values are ceilings, not requests."""

    vcpus: int
    ram_gb: int
    disk_gb: int


@dataclass(frozen=True)
class HostCaps:
    """Host-capacity table keyed by VM class (role)."""

    agent_vm: RoleCaps
    inference_vm: RoleCaps

    def __post_init__(self) -> None:
        for role, caps in (
            ("agent_vm", self.agent_vm),
            ("inference_vm", self.inference_vm),
        ):
            for field, value in (
                ("vcpus", caps.vcpus),
                ("ram_gb", caps.ram_gb),
                ("disk_gb", caps.disk_gb),
            ):
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ValueError(f"host_caps {role}.{field} must be an int, got {value!r}")
                if value < FLOORS[field]:
                    raise ValueError(
                        f"host_caps {role}.{field}={value} is below the structural floor "
                        f"{FLOORS[field]}"
                    )
                if value > ABSOLUTE_CAPS[field]:
                    raise ValueError(
                        f"host_caps {role}.{field}={value} exceeds the absolute cap "
                        f"{ABSOLUTE_CAPS[field]}"
                    )

    def for_role(self, role: str) -> RoleCaps:
        if role == "agent_vm":
            return self.agent_vm
        if role == "inference_vm":
            return self.inference_vm
        raise ValueError(f"unknown role {role!r}")

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "HostCaps":
        """Normalize a plain dict (or a HostCaps) into a validated HostCaps.

        Accepts ``{"agent_vm": {"vcpus": 8, "ram_gb": 16, "disk_gb": 64}, ...}``
        or ``{"vcpus": 8, "ram_gb": 16, "disk_gb": 64}`` (shared across roles).
        ``None`` falls back to :data:`DEFAULT_HOST_CAPS`.
        """
        if isinstance(d, HostCaps):
            return d
        if d is None:
            d = DEFAULT_HOST_CAPS
        if not isinstance(d, dict):
            raise ValueError(f"host_caps must be a dict or HostCaps, got {type(d).__name__}")

        if "agent_vm" in d or "inference_vm" in d:
            agent = d.get("agent_vm", DEFAULT_HOST_CAPS["agent_vm"])
            infer = d.get("inference_vm", DEFAULT_HOST_CAPS["inference_vm"])
            return cls(
                agent_vm=RoleCaps(**cls._extract(agent, "agent_vm")),
                inference_vm=RoleCaps(**cls._extract(infer, "inference_vm")),
            )

        # Shared capacity: same maxima for every role.
        shared = cls._extract(d, "shared")
        caps = RoleCaps(**shared)
        return cls(agent_vm=caps, inference_vm=caps)

    @staticmethod
    def _extract(d: Dict[str, Any], label: str) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for field in ("vcpus", "ram_gb", "disk_gb"):
            if field not in d:
                raise ValueError(f"host_caps[{label!r}] missing required key {field!r}")
            out[field] = d[field]
        return out


# ---------------------------------------------------------------------------
# Validator output structures (the LXD-facing spec)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AllocationSpec:
    """One validated VM class. This is the only shape handed to LXD."""

    type: str
    count: int
    vcpus: int
    ram_gb: int
    disk_gb: int
    passthrough_gpu: bool = False
    target_node: str = "auto"

    def __post_init__(self) -> None:
        if self.type not in KNOWN_TYPES:
            raise ValueError(f"unknown type {self.type!r}")
        for field_name in NUMERIC_FIELDS:
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{field_name} must be an int, got {value!r}")
            if value < FLOORS[field_name]:
                raise ValueError(
                    f"{field_name}={value} is below the floor {FLOORS[field_name]}"
                )
            if field_name != "count" and value > ABSOLUTE_CAPS[field_name]:
                raise ValueError(
                    f"{field_name}={value} exceeds the absolute cap "
                    f"{ABSOLUTE_CAPS[field_name]}"
                )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "count": self.count,
            "vcpus": self.vcpus,
            "ram_gb": self.ram_gb,
            "disk_gb": self.disk_gb,
            "passthrough_gpu": self.passthrough_gpu,
            "target_node": self.target_node,
        }


@dataclass(frozen=True)
class ClampDecision:
    """A single requested-vs-applied decision, for logging/explainability."""

    index: int
    field: str
    requested: Any  # raw value as submitted (pre-coercion)
    applied: Any  # final value actually applied
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "field": self.field,
            "decision": "clamped" if self.reason not in (REASON_DEFAULTED,) else "defaulted",
            "from": self.requested,
            "to": self.applied,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Rejection:
    """A component-level (or request-level) rejection. index is None when
    request-level."""

    index: Optional[int]
    reason: str
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "reason": self.reason, "detail": self.detail}


@dataclass
class ValidationResult:
    """Authoritative validator output.

    ``allocations`` is EMPTY whenever ``outcome == "rejected"`` (all-or-nothing,
    per controller-api.md §5.3). The raw LLM dicts are never returned; only
    freshly constructed :class:`AllocationSpec` objects are.
    """

    allocations: List[AllocationSpec] = field(default_factory=list)
    decisions: List[ClampDecision] = field(default_factory=list)
    rejections: List[Rejection] = field(default_factory=list)
    outcome: str = "accepted"  # accepted | clamped | rejected

    @property
    def safe(self) -> bool:
        """True only when the spec may proceed to LXD (accepted or clamped)."""
        return self.outcome in ("accepted", "clamped")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome,
            "allocations": [a.to_dict() for a in self.allocations],
            "decisions": [d.to_dict() for d in self.decisions],
            "rejections": [r.to_dict() for r in self.rejections],
        }
