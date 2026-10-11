"""Typed data structures for the model-driven operator decision step.

These are the ONLY structures that cross the inference-side boundary
(``termini.decision``): the adapter renders a :class:`DecisionInput` into a
prompt and turns the model's completion text into either a
:class:`DecisionResponse` or a typed :class:`DecisionError`. The raw model text
never leaves this package except inside a ``DecisionError.raw`` debug field.

Field names, enum values, and bounds follow the frozen contract
``docs/contracts/operator-decision.md``:

* §3  ``DecisionInput``  — the controller-assembled request payload (schema 1).
* §4  ``DecisionResponse`` — the single JSON object the model must emit.
* §5  ``ACTION_SPACE``   — the canonical action whitelist (single source of
  truth, shared by the adapter's prompt and the controller's guardrail).
* §10.1 ``FALLBACK_REASONS`` — the exact fallback-reason enum.

This module is **pure** (no network, no I/O, no model call), mirroring
``termini.intent.types``. It is deliberately *not* the security boundary: the
hard guardrail (G1–G8) lives controller-side in
``controller/app/operator/decision.py``. The adapter only parses and
light-repairs model output so it is well-shaped; the controller decides whether
it is *allowed*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Frozen bounds (operator-decision.md §4/§5/§12)
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1

# rationale: §4.1 — 1 .. 1024 chars, C0-safe.
RATIONALE_MIN_CHARS = 1
RATIONALE_MAX_CHARS = 1024

# confidence: §4.1 — 0.0 .. 1.0.
CONFIDENCE_MIN = 0.0
CONFIDENCE_MAX = 1.0

# instance.restart timeout_s: §5 — int 1 .. 120.
TIMEOUT_S_MIN = 1
TIMEOUT_S_MAX = 120

# Param defaults from ACTION_SPACE (§5).
DEFAULT_FORCE = True
DEFAULT_TIMEOUT_S = 30

# Contract §12 frozen defaults (consumed controller-side; mirrored here so the
# adapter and the controller cannot drift).
DEFAULT_DECISION_TIMEOUT_S = 30.0
DEFAULT_DECISION_MIN_CONFIDENCE = 0.5
DEFAULT_MAX_CONSECUTIVE_FALLBACKS = 3
DEFAULT_FALLBACK_COOLDOWN_S = 300

# The single ``instance_name`` sentinel (§5): the guardrail resolves it to the
# highest-``ram_used_bytes`` instance across the evidence window.
HEAVIEST = "heaviest"

# The demo's default LXD instance name when nothing resolves (§2.1).
DEFAULT_INSTANCE_NAME = "agent"


# ---------------------------------------------------------------------------
# Action space — the canonical, versioned whitelist (operator-decision.md §5)
# ---------------------------------------------------------------------------
#
# This is the SINGLE source of truth for both the adapter (which enumerates it
# in the prompt) and the controller guardrail (which validates against it). It
# must match ``LxdExecutor.execute``'s dispatch exactly:
#
#     instance.restart | instance.stop | instance.delete | image.prune
#
# Adding an action here requires a matching executor method and a playbook, and
# vice-versa.

ACTION_SPACE: Dict[str, Dict[str, Any]] = {
    "instance.restart": {
        "params": {
            "instance_name": {"type": "string", "required": True, "resolve": "instance"},
            "force": {"type": "bool", "required": False, "default": DEFAULT_FORCE},
            "timeout_s": {
                "type": "int",
                "required": False,
                "default": DEFAULT_TIMEOUT_S,
                "min": TIMEOUT_S_MIN,
                "max": TIMEOUT_S_MAX,
            },
        },
    },
    "instance.stop": {
        "params": {
            "instance_name": {"type": "string", "required": True, "resolve": "instance"},
            "force": {"type": "bool", "required": False, "default": DEFAULT_FORCE},
        },
    },
    "instance.delete": {
        "params": {
            "instance_name": {"type": "string", "required": True, "resolve": "instance"},
        },
    },
    "image.prune": {"params": {}},
}

# The four action ids, in declaration order. Used by the prompt builder.
ACTION_IDS: Tuple[str, ...] = tuple(ACTION_SPACE)


# ---------------------------------------------------------------------------
# Fallback reasons (operator-decision.md §10.1) — the exact enum
# ---------------------------------------------------------------------------

REASON_INFERENCE_UNAVAILABLE = "inference_unavailable"
REASON_INFERENCE_TIMEOUT = "inference_timeout"
REASON_PARSE_ERROR = "parse_error"
REASON_SCHEMA_VIOLATION = "schema_violation"
REASON_ACTION_NOT_ALLOWED = "action_not_allowed"
REASON_INVALID_TARGET = "invalid_target"
REASON_LOW_CONFIDENCE = "low_confidence"

FALLBACK_REASONS: Tuple[str, ...] = (
    REASON_INFERENCE_UNAVAILABLE,
    REASON_INFERENCE_TIMEOUT,
    REASON_PARSE_ERROR,
    REASON_SCHEMA_VIOLATION,
    REASON_ACTION_NOT_ALLOWED,
    REASON_INVALID_TARGET,
    REASON_LOW_CONFIDENCE,
)

# The subset of fallback reasons the *adapter* can itself produce. The others
# (``action_not_allowed``, ``invalid_target``, ``low_confidence``) are raised by
# the controller's guardrail (G3/G5/G6), not here.
ADAPTER_FALLBACK_REASONS: Tuple[str, ...] = (
    REASON_INFERENCE_UNAVAILABLE,
    REASON_INFERENCE_TIMEOUT,
    REASON_PARSE_ERROR,
    REASON_SCHEMA_VIOLATION,
)


# ---------------------------------------------------------------------------
# Action-space helpers (pure)
# ---------------------------------------------------------------------------

def params_schema(action_id: str) -> Dict[str, Dict[str, Any]]:
    """Return a copy of ``action_id``'s param schema, or ``{}`` if unknown."""
    spec = ACTION_SPACE.get(action_id)
    if not spec:
        return {}
    return dict(spec.get("params") or {})


def default_params(action_id: str) -> Dict[str, Any]:
    """The optional params (with their defaults) for an action.

    Required params without a default (``instance_name``) are omitted — the
    guardrail's G5 resolves/validates them (fallback ``invalid_target`` when
    unresolvable). Unknown actions yield ``{}``.
    """
    out: Dict[str, Any] = {}
    for name, spec in params_schema(action_id).items():
        if not spec.get("required") and "default" in spec:
            out[name] = spec["default"]
    return out


def action_ids_in(space: Any) -> List[str]:
    """Ordered action ids from a serialized ``allowed_action_space`` array.

    Accepts the §3 wire shape (``[{"action_id": ..., "params_schema": ...}]``)
    or a bare list of id strings. Returns ``[]`` for anything unusable.
    """
    if not isinstance(space, list):
        return []
    ids: List[str] = []
    for entry in space:
        if isinstance(entry, str):
            ids.append(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("action_id"), str):
            ids.append(entry["action_id"])
    return ids


# ---------------------------------------------------------------------------
# Request payload (operator-decision.md §3)
# ---------------------------------------------------------------------------

@dataclass
class DecisionInput:
    """The controller-assembled decision request (schema 1).

    Every field is C0 (``governance.md`` §7.2): it is derivable from the health
    event plus validated telemetry state, so it may appear in the (on-prem)
    model prompt. The adapter serializes this verbatim as compact JSON — no
    transformation, no C1/C2 content.
    """

    request_id: str
    project_id: str = ""
    node_id: str = ""
    rule_id: str = ""
    type: str = ""  # oom | disk_fail | disk_degrade | latency
    trigger: Dict[str, Any] = field(default_factory=dict)
    state: Dict[str, Any] = field(default_factory=dict)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    target_resource: Dict[str, Any] = field(default_factory=dict)
    allowed_action_space: List[Dict[str, Any]] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"DecisionInput.schema_version must be {SCHEMA_VERSION}, "
                f"got {self.schema_version!r}"
            )
        if not isinstance(self.request_id, str) or not self.request_id:
            raise ValueError("DecisionInput.request_id must be a non-empty string")

    def allowed_action_ids(self) -> List[str]:
        """The action ids the model may choose from (§3.1).

        When ``allowed_action_space`` is empty the full ``ACTION_SPACE`` is the
        whitelist; otherwise the declared ids are intersected with it so a
        mis-declared action can never be surfaced as allowed.
        """
        declared = action_ids_in(self.allowed_action_space)
        if not declared:
            return list(ACTION_IDS)
        return [a for a in declared if a in ACTION_SPACE]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "project_id": self.project_id,
            "node_id": self.node_id,
            "rule_id": self.rule_id,
            "type": self.type,
            "trigger": self.trigger,
            "state": self.state,
            "evidence": self.evidence,
            "target_resource": self.target_resource,
            "allowed_action_space": self.allowed_action_space,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DecisionInput":
        """Build a :class:`DecisionInput` from the §3 wire shape.

        Missing optional fields default; ``request_id`` is required and a
        wrong ``schema_version`` is rejected (``ValueError``).
        """
        if not isinstance(data, dict):
            raise ValueError(f"DecisionInput must be a dict, got {type(data).__name__}")
        return cls(
            request_id=data.get("request_id", ""),
            project_id=data.get("project_id", ""),
            node_id=data.get("node_id", ""),
            rule_id=data.get("rule_id", ""),
            type=data.get("type", ""),
            trigger=dict(data.get("trigger") or {}),
            state=dict(data.get("state") or {}),
            evidence=list(data.get("evidence") or []),
            target_resource=dict(data.get("target_resource") or {}),
            allowed_action_space=list(data.get("allowed_action_space") or []),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


# ---------------------------------------------------------------------------
# Response payload (operator-decision.md §4)
# ---------------------------------------------------------------------------

@dataclass
class DecisionResponse:
    """The model's decision, well-shaped and lightly repaired (schema 1).

    Produced by :func:`termini.decision.adapter.parse`. It is *not* yet
    authorized: the controller's guardrail (G3/G5/G6/G7) still has to accept or
    fall back. ``params`` already contains only keys from the chosen action's
    ``params_schema``, with optional params defaulted.
    """

    action_id: str
    confidence: float
    rationale: str
    params: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    request_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "action_id": self.action_id,
            "params": self.params,
            "confidence": self.confidence,
            "rationale": self.rationale,
        }
        if self.request_id is not None:
            out["request_id"] = self.request_id
        return out


# ---------------------------------------------------------------------------
# Typed error (operator-decision.md §6.2 / §10.1)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DecisionError:
    """A typed, non-fatal failure of the decision adapter.

    ``reason`` is exactly one of :data:`FALLBACK_REASONS`, so the controller can
    map it straight onto the deterministic fallback path. The adapter never
    fabricates an action: when the model output cannot be repaired into a
    well-shaped :class:`DecisionResponse`, it returns this instead of a guess.
    """

    reason: str
    detail: str = ""
    raw: Optional[str] = None

    def __post_init__(self) -> None:
        if self.reason not in FALLBACK_REASONS:
            raise ValueError(
                f"DecisionError.reason must be one of {FALLBACK_REASONS}, "
                f"got {self.reason!r}"
            )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"error": self.reason, "detail": self.detail}
        if self.raw is not None:
            out["raw"] = self.raw
        return out
