"""The operator decision step, behind a ``DecisionProvider`` seam.

Implements the controller half of the amended operator decision-step contract
(``docs/contracts/operator-decision.md``):

* ``DecisionProvider`` — the seam the loop calls instead of ``playbook.choose``.
* ``DeterministicDecisionProvider`` — wraps ``playbook.choose`` verbatim; the
  default and the mandatory fallback.
* ``ModelDecisionProvider`` — builds a ``DecisionInput``, calls the inference
  adapter (``termini.decision.decide``, the ``agents`` workstream's module), and
  runs the hard guardrail; falls back to the deterministic provider on any
  failure.
* ``Guardrail`` (G1–G8) — the pure, LLM-free validation layer. No model output
  reaches ``LxdExecutor`` without passing every rule.

The guardrail is the enforcement point (contract §7): the adapter performs
parse + light repair only (§6.2) and deliberately does not enforce the
whitelist (G3), param bounds / target resolution (G5), the confidence gate (G6)
or the C2 scan (G7). An out-of-whitelist action, an out-of-range ``timeout_s``,
or a low confidence all arrive here as a well-shaped ``DecisionResponse`` and
are rejected/clamped/gated here. The adapter's ``decide()`` never raises for
transport/latency/parse problems — it returns a ``DecisionError`` whose
``reason`` is one of the §10.1 codes, mapped straight onto the fallback.

The guardrail performs no network, no model call, no subprocess, and no I/O. It
imports only :mod:`termini.decision.types` and the operator models/governance
scanner.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, runtime_checkable

from .. import ulid
from .. import governance
from .. import fields as F
from .models import HealthEvent, RemediationAction
from . import playbook
from . import detect

# The controller runs both from the repo root (PYTHONPATH=/app in the image;
# `python3 -m unittest` in CI) and from `controller/` (`make run`). Ensure the
# repo-root `termini` package is importable in the latter case, mirroring
# server.py's `_add_repo_root`.
try:
    from termini.decision.types import (  # noqa: F401
        ACTION_IDS,
        ACTION_SPACE,
        DEFAULT_DECISION_MIN_CONFIDENCE,
        DEFAULT_DECISION_TIMEOUT_S,
        DEFAULT_FALLBACK_COOLDOWN_S,
        DEFAULT_MAX_CONSECUTIVE_FALLBACKS,
        FALLBACK_REASONS,
        HEAVIEST,
        ACTIONS_MAX_ENTRIES,
        ACTION_NOTE_MAX_CHARS,
        ACTION_TARGET_MAX_CHARS,
        EVIDENCE_FIELD_MAX_CHARS,
        EVIDENCE_FIELD_PATTERN,
        EVIDENCE_MAX_ENTRIES,
        EVIDENCE_VALUE_STR_MAX_CHARS,
        EXPLANATION_MAX_CHARS,
        INCIDENT_GENERATED_FALLBACK,
        INCIDENT_GENERATED_MODEL,
        INCIDENT_TITLE_MAX_CHARS,
        TRACE_DECIDED_BY_DETERMINISTIC,
        TRACE_DECIDED_BY_FALLBACK,
        TRACE_DECIDED_BY_MODEL,
        TRACE_MAX_STEPS,
        TRACE_REPLY_MAX_CHARS,
        TRACE_STATE_CLAMPED,
        TRACE_STATE_DROPPED,
        TRACE_STATE_FALLBACK,
        TRACE_STATE_OK,
        TRACE_STATE_REJECTED,
        TRACE_STATE_SKIPPED,
        TRACE_STEP_DETAIL_MAX_CHARS,
        TRACE_VERSION,
        RATIONALE_MAX_CHARS,
        SCHEMA_VERSION,
        TIMEOUT_S_MAX,
        TIMEOUT_S_MIN,
        DecisionError,
        DecisionInput,
        DecisionResponse,
        params_schema,
    )
except ImportError:  # pragma: no cover - exercised only when run from controller/
    import os
    import sys

    # decision.py is at <repo>/controller/app/operator/decision.py; the repo
    # root is four parents up (operator -> app -> controller -> repo root).
    _p = os.path.abspath(__file__)
    for _ in range(4):
        _p = os.path.dirname(_p)
    if _p not in sys.path:
        sys.path.insert(0, _p)
    from termini.decision.types import (  # noqa: F401
        ACTION_IDS,
        ACTION_SPACE,
        DEFAULT_DECISION_MIN_CONFIDENCE,
        DEFAULT_DECISION_TIMEOUT_S,
        DEFAULT_FALLBACK_COOLDOWN_S,
        DEFAULT_MAX_CONSECUTIVE_FALLBACKS,
        FALLBACK_REASONS,
        HEAVIEST,
        ACTIONS_MAX_ENTRIES,
        ACTION_NOTE_MAX_CHARS,
        ACTION_TARGET_MAX_CHARS,
        EVIDENCE_FIELD_MAX_CHARS,
        EVIDENCE_FIELD_PATTERN,
        EVIDENCE_MAX_ENTRIES,
        EVIDENCE_VALUE_STR_MAX_CHARS,
        EXPLANATION_MAX_CHARS,
        INCIDENT_GENERATED_FALLBACK,
        INCIDENT_GENERATED_MODEL,
        INCIDENT_TITLE_MAX_CHARS,
        TRACE_DECIDED_BY_DETERMINISTIC,
        TRACE_DECIDED_BY_FALLBACK,
        TRACE_DECIDED_BY_MODEL,
        TRACE_MAX_STEPS,
        TRACE_REPLY_MAX_CHARS,
        TRACE_STATE_CLAMPED,
        TRACE_STATE_DROPPED,
        TRACE_STATE_FALLBACK,
        TRACE_STATE_OK,
        TRACE_STATE_REJECTED,
        TRACE_STATE_SKIPPED,
        TRACE_STEP_DETAIL_MAX_CHARS,
        TRACE_VERSION,
        RATIONALE_MAX_CHARS,
        SCHEMA_VERSION,
        TIMEOUT_S_MAX,
        TIMEOUT_S_MIN,
        DecisionError,
        DecisionInput,
        DecisionResponse,
        params_schema,
    )

log = logging.getLogger("termini.operator.decision")


# --------------------------------------------------------------------------- #
# Seam types (operator-decision.md §2)
# --------------------------------------------------------------------------- #

@dataclass
class DecisionContext:
    """Everything a provider needs to decide (all C0, §2.1).

    ``project_id`` is C0 loop state carried so the request payload (§3) can be
    scoped; the node id is taken from ``event.node_id``. Everything else matches
    the §2.1 table.
    """

    event: HealthEvent
    state: dict
    evidence: list
    instances: list
    instance_names: dict
    default_instance_name: str
    allowed_actions: list
    project_id: str = ""


@dataclass
class DecisionResult:
    """The loop's only output of the decision step (§2.1)."""

    action: RemediationAction | None
    source: str                        # deterministic | model | fallback
    confidence: float | None = None
    fallback_reason: str | None = None  # set iff source == "fallback"
    guardrail: list = field(default_factory=list)
    # The §4.2 incident record, or None in deterministic mode (§8.1). Always
    # carries `generated` ∈ {model, fallback} when present, so the reader can
    # never mistake a controller-built record for model output.
    incident: dict | None = None
    # The §4.3 decision trace (a DecisionTrace), or None in deterministic mode
    # (§8.1: no trace columns are written). The loop appends the `executed` and
    # `audit_recorded` steps to it after the audit, then persists it.
    trace: "DecisionTrace | None" = None


@runtime_checkable
class DecisionProvider(Protocol):
    def decide(self, event: HealthEvent, context: DecisionContext) -> DecisionResult:
        ...


# --------------------------------------------------------------------------- #
# Decision trace (operator-decision.md §4.3) — captured + sanitized at the site
# --------------------------------------------------------------------------- #

def _now_iso_ms() -> str:
    """Current UTC time as RFC3339 with millisecond precision (matches triggers)."""
    import datetime
    dt = datetime.datetime.now(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _c0_field_names(sample: Any) -> list[str]:
    """The C0 field names a sample carries, for the ``evidence_window`` step.

    C1 identifiers (``hostname``, ``ip_local``, ``mac_address``, ``project_id``)
    are deliberately excluded: the trace must not echo identifiers, only name
    the C0 fields the prompt carried (governance.md §4.1/§4.2).
    """
    if not isinstance(sample, dict):
        return []
    names = []
    for key in sample:
        if key in F.SAMPLE_FIELDS and F.SAMPLE_FIELDS[key].get("cclass") == "C0":
            names.append(key)
        elif key in F.SAMPLE_CONTAINERS:
            names.append(key)
    return sorted(names)


def sha256_display(digest: str) -> str:
    """The payload sha256, grouped in 8-hex chunks.

    A hex digest is C0, but `governance.scan_c2`'s base64 heuristic matches any
    40+ `[A-Za-z0-9+/]` run, so a contiguous 64-hex sha256 would be dropped as
    a base64 blob. Grouping it keeps the full digest while breaking that run.
    """
    if not isinstance(digest, str) or not digest:
        return ""
    return "-".join(digest[i:i + 8] for i in range(0, len(digest), 8))


def safe_int_display(value: Any) -> str:
    """A non-negative integer formatted so digit-run heuristics cannot misread it.

    `governance.scan_c2` flags any 10-15 digit run as a phone number and any
    13-16 digit+separator run as a card number. Controller-generated metrics
    (a slow detection latency in ms, a token count) can legitimately reach that
    size, so large values are grouped with thousands separators, which break
    both patterns without losing the value.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "0"
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        return "0"
    if v < 1_000_000_000:  # 10+ digits read as a phone number to scan_c2
        return str(v)
    return f"{v:,}"


class DecisionTrace:
    """The §4.3 decision trace — one ordered array of ``{step, ts, label,
    detail, state}`` steps, bounded and sanitized with the SAME scanner G7/G9
    use (``governance.scan_c2`` — never a parallel path).

    * ≤ ``TRACE_MAX_STEPS`` steps; per-step ``detail`` ≤ 512 chars; the model
      reply step's text ≤ 4096 chars (truncation flagged).
    * A C2 hit in a step's ``detail`` **drops** that step and records the drop
      in ``drops`` (merged into the guardrail list, and emitted as a ``gates``
      step so it is persisted).
    * A C2 hit in the model reply truncates the reply step to ``[dropped: C2]``
      with ``state=rejected`` rather than storing the text.

    ``decided_by`` makes the decider visible in-band: ``model``, ``fallback`` or
    ``deterministic`` (§4.3/§6 requirement). A fallback trace is a controller
    trace and must say so.
    """

    def __init__(self, *, decided_by: str = TRACE_DECIDED_BY_DETERMINISTIC,
                 now_fn=None):
        self.decided_by = decided_by
        self.steps: list[dict] = []
        self.drops: list[dict] = []  # G9 drop records, merged into the guardrail list
        self._now_fn = now_fn or _now_iso_ms

    @property
    def trace_version(self) -> int:
        return TRACE_VERSION

    # ------------------------------------------------------------- appending
    def add(self, step: str, label: str, detail: Any = "",
            state: str = TRACE_STATE_OK) -> dict | None:
        """Append a generic step. Returns the step, or ``None`` when the step
        was dropped (a C2 hit in ``detail``); the drop is recorded in ``drops``
        and mirrored into the guardrail list by the provider/loop."""
        if len(self.steps) >= TRACE_MAX_STEPS:
            return None
        detail = self._clamp_detail(detail)
        c2 = governance.scan_c2(detail)
        if c2:
            self.drops.append({"rule": "G9", "decision": "drop",
                               "field": f"trace.{step}.detail",
                               "detail": f"C2 signature in trace step {step}: {c2[0]}"})
            return None
        return self._append(step, label, detail, state)

    def add_reply(self, label: str, text: Any,
                  state: str = TRACE_STATE_OK) -> dict | None:
        """Append the ``reply_received`` step (the raw model reply, bounded).

        Unlike ``add``, a C2 hit does NOT drop the step: the text is replaced
        with ``[dropped: C2]`` and ``state`` becomes ``rejected`` (§4.3)."""
        if len(self.steps) >= TRACE_MAX_STEPS:
            return None
        text, truncated = self._clamp_reply(text)
        c2 = governance.scan_c2(text)
        if c2:
            self.drops.append({"rule": "G9", "decision": "drop",
                               "field": "trace.reply_received.detail",
                               "detail": f"C2 signature in model reply: {c2[0]}"})
            return self._append("reply_received", label, "[dropped: C2]",
                                TRACE_STATE_REJECTED, truncated=truncated)
        return self._append("reply_received", label, text, state,
                            truncated=truncated)

    # -------------------------------------------------------------- internals
    def _append(self, step: str, label: str, detail: str, state: str,
                **extra) -> dict:
        s = {"step": step, "ts": self._now_fn(), "label": label,
             "detail": detail, "state": state}
        s.update(extra)
        self.steps.append(s)
        return s

    @staticmethod
    def _clamp_detail(detail: Any) -> str:
        if not isinstance(detail, str):
            detail = "" if detail is None else str(detail)
        if len(detail) > TRACE_STEP_DETAIL_MAX_CHARS:
            detail = detail[:TRACE_STEP_DETAIL_MAX_CHARS]
        return detail

    @staticmethod
    def _clamp_reply(text: Any) -> tuple[str, bool]:
        if not isinstance(text, str):
            text = "" if text is None else str(text)
        truncated = len(text) > TRACE_REPLY_MAX_CHARS
        if truncated:
            text = text[:TRACE_REPLY_MAX_CHARS]
        return text, truncated

    def to_dict(self) -> dict:
        return {"trace_version": self.trace_version,
                "decided_by": self.decided_by,
                "steps": list(self.steps)}


# --------------------------------------------------------------------------- #
# Request builders (operator-decision.md §3, §5)
# --------------------------------------------------------------------------- #

def _params_schema_summary(action_id: str) -> dict:
    """The §3 wire form of an action's param schema (``"int 1..120"`` etc.)."""
    out: dict = {}
    for name, spec in params_schema(action_id).items():
        ptype = spec.get("type", "string")
        if ptype == "int" and "min" in spec and "max" in spec:
            out[name] = f"int {spec['min']}..{spec['max']}"
        else:
            out[name] = ptype
    return out


def build_allowed_action_space(allowed_actions: Iterable[str] | None = None) -> list[dict]:
    """The whitelist the model may pick from: §5 ``ACTION_SPACE`` intersected
    with ``allowed_actions`` (default: all four actions), in the §3 wire shape."""
    ids = list(allowed_actions) if allowed_actions else list(ACTION_IDS)
    out: list[dict] = []
    for action_id in ids:
        if action_id not in ACTION_SPACE:
            continue
        out.append({"action_id": action_id, "params_schema": _params_schema_summary(action_id)})
    return out


# The monotonic cumulative counters whose diagnostic signal is the DELTA over the
# evidence window, not the raw total (research t_cae0dfa8 rows 2/3/7; the infra
# collector hand-off's "cumulative, not rates"). Each delta is computed by the
# controller and labelled derived in the `derived` object — never presented as a
# raw sample.
_CUMULATIVE_COUNTERS: tuple[str, ...] = (
    "oom_kill_count", "pgmajfault_count", "tcp_retransmit_segs",
)


def _is_counter(value: Any) -> bool:
    """True when ``value`` is a usable cumulative counter (finite non-negative)."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and value >= 0
    )


def build_window_deltas(evidence: list) -> dict:
    """Derived deltas over the evidence window for the cumulative counters (§3.1).

    Each delta is ``newest - oldest`` over the (oldest-first, already capped)
    evidence window, emitted only when BOTH ends carry a finite non-negative
    counter and the counter did not reset (``newest >= oldest`` — a node reboot
    or a counter wrap is omitted, never a negative delta, and a missing field is
    omitted, never a fabricated 0, matching the collector's omit-not-fabricate
    rule). Returns at least ``{"window_samples": n}``.

    Pure and C0-only: the result is a flat object of non-negative ints built
    from the same C0 samples the provider already hands to the model.
    """
    samples = [s for s in (evidence or []) if isinstance(s, dict)]
    out: dict = {"window_samples": len(samples)}
    if len(samples) < 2:
        return out
    oldest, newest = samples[0], samples[-1]
    for name in _CUMULATIVE_COUNTERS:
        a = oldest.get(name)
        b = newest.get(name)
        if not (_is_counter(a) and _is_counter(b)):
            continue
        delta = int(b) - int(a)  # type: ignore[operator,arg-type]
        if delta < 0:
            continue
        out[f"{name}_delta"] = delta
    return out


def build_decision_input(context: DecisionContext, request_id: str | None = None) -> DecisionInput:
    """Assemble the §3 ``DecisionInput`` from a ``DecisionContext``."""
    event = context.event
    trigger = {
        "metric": event.metric,
        "value": event.value,
        "threshold": event.threshold,
        "fired_at": event.fired_at,
        "breach_started_at": getattr(event, "breach_started_at", None),
        "episode": int(getattr(event, "episode", 0) or 0),
    }
    return DecisionInput(
        request_id=request_id or ulid.new_ulid(),
        project_id=context.project_id or "",
        node_id=event.node_id,
        rule_id=event.rule_id,
        type=event.type,
        trigger=trigger,
        state=dict(context.state or {}),
        evidence=list(context.evidence or []),
        target_resource={
            "node_id": event.node_id,
            "instances": list(context.instances or []),
        },
        allowed_action_space=build_allowed_action_space(context.allowed_actions),
        derived=build_window_deltas(context.evidence or []),
        schema_version=SCHEMA_VERSION,
    )


# --------------------------------------------------------------------------- #
# Deterministic provider (operator-decision.md §8)
# --------------------------------------------------------------------------- #

class DeterministicDecisionProvider:
    """``playbook.choose`` with ``source="deterministic"``, ``confidence=None``.

    Byte-identical to today's behavior so existing operator-loop tests pass
    unchanged in deterministic mode — with one deliberate exception: when the
    caller supplies a `DecisionContext`, its `instance_names` are merged over the
    configured map. The loop fills that mapping from the node's LIVE LXD
    inventory (`loop._live_instance_names`), and a resolution that ignored it
    would name a vm_id as if it were an instance name and be refused by the
    executor (`target_missing`) — the deterministic fast-path the demo leans on
    would stop remediating real nodes.
    """

    def __init__(self, *, playbooks: dict, instance_names: dict | None = None,
                 default_instance_name: str = "agent"):
        self.playbooks = playbooks
        self.instance_names = instance_names or {}
        self.default_instance_name = default_instance_name

    def decide(self, event: HealthEvent, context: DecisionContext | None = None) -> DecisionResult:
        names = dict(self.instance_names)
        default = self.default_instance_name
        if context is not None:
            names.update(context.instance_names or {})
            default = context.default_instance_name or default
        action = playbook.choose(event, self.playbooks, names, default)
        return DecisionResult(
            action=action, source="deterministic", confidence=None,
            fallback_reason=None, guardrail=[],
        )


# --------------------------------------------------------------------------- #
# Hard guardrail (operator-decision.md §7) — pure, LLM-free.
# --------------------------------------------------------------------------- #

@dataclass
class GuardrailResult:
    ok: bool
    action: RemediationAction | None = None
    confidence: float | None = None
    reject_reason: str | None = None    # one of FALLBACK_REASONS when not ok
    decisions: list = field(default_factory=list)
    # The validated §4.2 incident record (G9), or None when the model emitted
    # none. Carries `generated: "model"` — set by the guardrail, never by the
    # adapter, so a record can never be dressed up as model output.
    incident: dict | None = None


def _heaviest_vm_id(evidence: list) -> str | None:
    """vm_id of the running instance with the most used RAM across `evidence`
    (mirrors playbook._heaviest_instance so model and deterministic resolution
    of "heaviest" cannot drift)."""
    best_ram = -1.0
    best_id = None
    for sample in evidence:
        instances = sample.get("lxd_instances") if isinstance(sample, dict) else None
        if not isinstance(instances, list):
            continue
        for inst in instances:
            if not isinstance(inst, dict):
                continue
            ram = inst.get("ram_used_bytes")
            if not isinstance(ram, (int, float)) or isinstance(ram, bool):
                continue
            if ram > best_ram:
                best_ram = ram
                best_id = inst.get("vm_id")
    return best_id


def _resolve_instance(value: Any, evidence: list, instances: list,
                      instance_names: dict, default: str) -> tuple[str | None, str | None]:
    """Resolve a model-supplied target to an LXD instance name (§5).

    ``HEAVIEST`` resolves to the highest-``ram_used_bytes`` instance across the
    evidence window, mapped ``vm_id -> name`` via ``instance_names``; otherwise
    the value must be a ``vm_id``/``instance_name`` present in
    ``target_resource.instances[]``. Returns ``(instance_name, vm_id)`` or
    ``(None, None)`` when unresolvable.
    """
    names = instance_names or {}
    inst_vm_ids = {
        i.get("vm_id") for i in instances
        if isinstance(i, dict) and i.get("vm_id")
    }
    if value == HEAVIEST:
        vm_id = _heaviest_vm_id(evidence)
        name = names.get(vm_id, vm_id) if vm_id else default
        return name, vm_id
    if isinstance(value, str) and value in inst_vm_ids:
        return names.get(value, value), value
    # Reverse lookup: an instance_name that maps to a present vm_id.
    if isinstance(value, str):
        for vm_id, name in names.items():
            if name == value and vm_id in inst_vm_ids:
                return name, vm_id
    return None, None


def _coerce_bool(value: Any, default: bool = True) -> bool:
    return value if isinstance(value, bool) else default


def _coerce_int_clamped(value: Any, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(minimum, min(maximum, value))
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return max(minimum, min(maximum, int(value)))
    return None


# --------------------------------------------------------------------------- #
# G9 — incident-record sanitization (§4.2, §7). Pure, LLM-free.
# --------------------------------------------------------------------------- #

_EVIDENCE_FIELD_RE = re.compile(EVIDENCE_FIELD_PATTERN)


def _incident_entry_strings(entry: dict) -> list[str]:
    """The strings inside one incident entry, for the C2 scan (G9).

    Every incident string is scanned the same way G7 scans `rationale`: a value
    that carries a C2 signature (a log line, an `argv`, an env var, a credential,
    a file path blob) is evidence the record cannot be trusted, so the entry is
    dropped. The `field` value is excluded — it is already constrained to the
    `^[a-z0-9_]+(\\.[a-z0-9_]+)*$` name pattern, which cannot carry prose.
    """
    out: list[str] = []
    for key in ("value", "ts", "target", "note"):
        v = entry.get(key)
        if isinstance(v, str):
            out.append(v)
    return out


def _sanitize_incident(resp: DecisionResponse, decisions: list[dict]) -> tuple[dict | None, str | None]:
    """G9: validate + clamp the §4.2 incident record. Never rejects the decision
    over an entry — a malformed/C2-carrying evidence or action entry is dropped
    and recorded — but a C2 hit in the title or explanation is a hard fallback
    (`schema_violation`), exactly as G7 treats `rationale`.

    Returns ``(incident, reject_reason)``. ``incident`` is the record to persist
    (``generated: "model"``), or ``None`` when the model emitted none of the four
    fields (a valid, record-less response). ``reject_reason`` is set only for the
    hard title/explanation fallback.
    """
    # Bounds (§4.2). The adapter already repairs these; re-run here because the
    # guardrail may receive a raw dict through `_response_from_dict`.
    title = resp.incident_title if isinstance(resp.incident_title, str) else ""
    explanation = resp.explanation if isinstance(resp.explanation, str) else ""
    if len(title) > INCIDENT_TITLE_MAX_CHARS:
        decisions.append({"rule": "G9", "decision": "clamp", "field": "incident_title",
                          "from": len(title), "to": INCIDENT_TITLE_MAX_CHARS})
        title = title[:INCIDENT_TITLE_MAX_CHARS]
    if len(explanation) > EXPLANATION_MAX_CHARS:
        decisions.append({"rule": "G9", "decision": "clamp", "field": "explanation",
                          "from": len(explanation), "to": EXPLANATION_MAX_CHARS})
        explanation = explanation[:EXPLANATION_MAX_CHARS]

    # Title/explanation C2 → hard fallback (the same scan G7 runs on rationale).
    title_c2 = governance.scan_c2(title)
    if title_c2:
        decisions.append({"rule": "G9", "decision": "reject", "field": "incident_title",
                          "reason": "schema_violation",
                          "detail": f"C2 signature in incident_title: {title_c2[0]}"})
        return None, "schema_violation"
    explanation_c2 = governance.scan_c2(explanation)
    if explanation_c2:
        decisions.append({"rule": "G9", "decision": "reject", "field": "explanation",
                          "reason": "schema_violation",
                          "detail": f"C2 signature in explanation: {explanation_c2[0]}"})
        return None, "schema_violation"

    evidence_out: list[dict] = []
    raw_evidence = resp.evidence if isinstance(resp.evidence, list) else []
    if len(raw_evidence) > EVIDENCE_MAX_ENTRIES:
        decisions.append({"rule": "G9", "decision": "clamp", "field": "evidence",
                          "from": len(raw_evidence), "to": EVIDENCE_MAX_ENTRIES})
        raw_evidence = raw_evidence[:EVIDENCE_MAX_ENTRIES]
    for i, entry in enumerate(raw_evidence):
        if not isinstance(entry, dict):
            decisions.append({"rule": "G9", "decision": "drop", "field": f"evidence[{i}]",
                              "detail": "not an object"})
            continue
        field = entry.get("field")
        if not isinstance(field, str):
            decisions.append({"rule": "G9", "decision": "drop", "field": f"evidence[{i}].field",
                              "detail": "missing/non-string field"})
            continue
        if len(field) > EVIDENCE_FIELD_MAX_CHARS or not _EVIDENCE_FIELD_RE.match(field):
            decisions.append({"rule": "G9", "decision": "drop", "field": f"evidence[{i}].field",
                              "detail": f"field {field!r} fails the name pattern"})
            continue
        value = entry.get("value")
        if isinstance(value, bool):
            pass
        elif isinstance(value, (int, float)):
            if not math.isfinite(float(value)):
                decisions.append({"rule": "G9", "decision": "drop",
                                  "field": f"evidence[{i}].value",
                                  "detail": "non-finite number"})
                continue
        elif isinstance(value, str):
            value = value[:EVIDENCE_VALUE_STR_MAX_CHARS]
        else:
            decisions.append({"rule": "G9", "decision": "drop", "field": f"evidence[{i}].value",
                              "detail": f"value type {type(value).__name__} not allowed"})
            continue
        # C2 scan over every string in the entry (value/ts); field is name-only.
        c2 = governance.scan_c2(_incident_entry_strings(entry))
        if c2:
            decisions.append({"rule": "G9", "decision": "drop", "field": f"evidence[{i}]",
                              "detail": f"C2 signature in entry: {c2[0]}"})
            continue
        row: dict = {"field": field, "value": value}
        seq = entry.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0:
            row["seq"] = seq
        ts = entry.get("ts")
        if isinstance(ts, str) and ts.strip():
            row["ts"] = ts.strip()
        evidence_out.append(row)

    actions_out: list[dict] = []
    raw_actions = resp.actions if isinstance(resp.actions, list) else []
    if len(raw_actions) > ACTIONS_MAX_ENTRIES:
        decisions.append({"rule": "G9", "decision": "clamp", "field": "actions",
                          "from": len(raw_actions), "to": ACTIONS_MAX_ENTRIES})
        raw_actions = raw_actions[:ACTIONS_MAX_ENTRIES]
    for i, entry in enumerate(raw_actions):
        if not isinstance(entry, dict):
            decisions.append({"rule": "G9", "decision": "drop", "field": f"actions[{i}]",
                              "detail": "not an object"})
            continue
        kind = entry.get("action_kind")
        if not isinstance(kind, str) or kind not in ACTION_SPACE:
            decisions.append({"rule": "G9", "decision": "drop", "field": f"actions[{i}].action_kind",
                              "detail": f"action_kind {kind!r} not in ACTION_SPACE"})
            continue
        c2 = governance.scan_c2(_incident_entry_strings(entry))
        if c2:
            decisions.append({"rule": "G9", "decision": "drop", "field": f"actions[{i}]",
                              "detail": f"C2 signature in entry: {c2[0]}"})
            continue
        row: dict = {"action_kind": kind}
        target = entry.get("target")
        if isinstance(target, str) and target.strip():
            row["target"] = target.strip()[:ACTION_TARGET_MAX_CHARS]
        note = entry.get("note")
        if isinstance(note, str) and note.strip():
            row["note"] = note.strip()[:ACTION_NOTE_MAX_CHARS]
        actions_out.append(row)

    if not (title or explanation or evidence_out or actions_out):
        return None, None

    return {
        "incident_title": title,
        "explanation": explanation,
        "evidence": evidence_out,
        "actions": actions_out,
        "generated": INCIDENT_GENERATED_MODEL,
    }, None


def _fallback_read_only_mounts(event: HealthEvent) -> list[str]:
    """Read-only disk mount paths from an event's own C0 evidence (sample tail).

    Used by `build_fallback_incident` so a disk-fail fallback names the mount(s)
    without touching model output. Returns `[]` when no sample carries one.
    """
    mounts: list[str] = []
    evidence = event.evidence if isinstance(event.evidence, list) else []
    for sample in reversed(evidence):
        if not isinstance(sample, dict):
            continue
        for entry in sample.get("disk") or []:
            if isinstance(entry, dict) and entry.get("mount_ro") is True:
                path = str(entry.get("path") or entry.get("mount") or "unknown")
                if path not in mounts:
                    mounts.append(path)
        if mounts:
            break
    return mounts


def build_fallback_incident(event: HealthEvent, action: RemediationAction | None,
                            reason: str) -> dict:
    """The §8.1 labelled fallback record — built ONLY from the fired event's C0
    fields, never from model output, and never dressed up as model output.

    A fallback that silently dropped the "why" would be the same defect as a
    blank dashboard: the reader could not tell "no incident" from "no model". So
    the fallback still writes a record, and it is *labelled* so a reader can
    never mistake it for the model's own answer:

      * ``generated: "fallback"``;
      * the explanation names the fallback reason in the text itself;
      * ``evidence[]`` holds only the event's own C0 fields (metric, value,
        threshold, sample ``seq``) — never a fabricated sample, never a model
        sentence.
    """
    reason = reason if reason in FALLBACK_REASONS else "inference_unavailable"
    title = f"{event.type} on node {event.node_id}".strip()
    title = title[:INCIDENT_TITLE_MAX_CHARS]

    kind = action.kind if action is not None else "no action"
    target = ""
    if action is not None and isinstance(action.params, dict):
        target = str(action.params.get("instance_name") or "").strip()
    # F05: a disk-fail fallback must NAME the mount and the read-only cause, and
    # must not claim recovery — the workload is fenced, not fixed. The mount path
    # comes from the event's own C0 evidence (the sample tail), never from model
    # output.
    disk_mounts = _fallback_read_only_mounts(event)
    if disk_mounts:
        explanation = (
            f"model unavailable ({reason}); disk remounted read-only on "
            f"{', '.join(disk_mounts)} (mount_ro=true) — the deterministic playbook "
            f"chose {kind}"
            + (f" on {target}" if target else "")
            + f" for rule {event.rule_id}. The workload is fenced; it is not recovered."
        )
    else:
        explanation = (
            f"model unavailable ({reason}); the deterministic playbook chose {kind}"
            + (f" on {target}" if target else "")
            + f" for rule {event.rule_id}"
        )
    explanation = explanation[:EXPLANATION_MAX_CHARS]

    evidence: list[dict] = []
    value: Any = event.value
    if not (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value))):
        value = event.threshold  # a C0 human-readable description, still event-derived
    entry: dict = {"field": event.metric, "value": value}
    seq = None
    if event.evidence:
        last = event.evidence[-1] if isinstance(event.evidence, list) else None
        if isinstance(last, dict):
            s = last.get("seq")
            if isinstance(s, int) and not isinstance(s, bool) and s >= 0:
                seq = s
    if seq is not None:
        entry["seq"] = seq
    evidence.append(entry)

    actions: list[dict] = []
    if action is not None:
        act: dict = {"action_kind": action.kind}
        if target:
            act["target"] = target
        actions.append(act)

    return {
        "incident_title": title,
        "explanation": explanation,
        "evidence": evidence,
        "actions": actions,
        "generated": INCIDENT_GENERATED_FALLBACK,
    }


class Guardrail:
    """Validates a model decision response (G1–G8) and builds the safe action.

    G4/G5 clamp (never reject on numeric/unknown-key issues); G1/G2/G3/G6/G7
    are hard fallbacks; G8 (idempotency) is deferred to the loop, which runs the
    resulting action through the existing idempotency gate unchanged.
    """

    def __init__(self, *, min_confidence: float = DEFAULT_DECISION_MIN_CONFIDENCE,
                 allowed_actions: Iterable[str] | None = None,
                 instance_names: dict | None = None,
                 default_instance_name: str = "agent"):
        self.min_confidence = min_confidence
        self.allowed_actions = list(allowed_actions) if allowed_actions else list(ACTION_IDS)
        self.instance_names = instance_names or {}
        self.default_instance_name = default_instance_name

    def validate(self, response: Any, context: DecisionContext) -> GuardrailResult:
        decisions: list[dict] = []

        # G1 — JSON parse (whole output). Handles a raw string/dict defensively;
        # the adapter normally returns a DecisionResponse already.
        resp, err = self._coerce_response(response)
        if err is not None:
            reason, detail = err
            decisions.append({"rule": "G1", "decision": "reject", "field": "output",
                              "reason": reason, "detail": detail})
            return GuardrailResult(ok=False, reject_reason=reason, decisions=decisions)
        if resp is None:  # defensive — unreachable by _coerce_response's contract
            decisions.append({"rule": "G1", "decision": "reject", "field": "output",
                              "reason": "parse_error", "detail": "no decision response"})
            return GuardrailResult(ok=False, reject_reason="parse_error", decisions=decisions)

        # G2 — schema validation (required fields + types).
        if resp.schema_version != SCHEMA_VERSION:
            decisions.append({"rule": "G2", "decision": "reject", "field": "schema_version",
                              "reason": "schema_violation",
                              "detail": f"schema_version {resp.schema_version!r} != {SCHEMA_VERSION}"})
            return GuardrailResult(ok=False, reject_reason="schema_violation", decisions=decisions)
        if not isinstance(resp.action_id, str) or not resp.action_id.strip():
            decisions.append({"rule": "G2", "decision": "reject", "field": "action_id",
                              "reason": "schema_violation", "detail": "action_id missing/empty"})
            return GuardrailResult(ok=False, reject_reason="schema_violation", decisions=decisions)
        if not isinstance(resp.params, dict):
            decisions.append({"rule": "G2", "decision": "reject", "field": "params",
                              "reason": "schema_violation", "detail": "params must be an object"})
            return GuardrailResult(ok=False, reject_reason="schema_violation", decisions=decisions)
        if isinstance(resp.confidence, bool) or not isinstance(resp.confidence, (int, float)):
            decisions.append({"rule": "G2", "decision": "reject", "field": "confidence",
                              "reason": "schema_violation", "detail": "confidence must be a number"})
            return GuardrailResult(ok=False, reject_reason="schema_violation", decisions=decisions)

        # G3 — action whitelist.
        if resp.action_id not in ACTION_SPACE or resp.action_id not in self.allowed_actions:
            decisions.append({"rule": "G3", "decision": "reject", "field": "action_id",
                              "reason": "action_not_allowed",
                              "detail": f"action_id {resp.action_id!r} not in allowed action space"})
            return GuardrailResult(ok=False, reject_reason="action_not_allowed", decisions=decisions)

        schema = params_schema(resp.action_id)

        # G4 — param key whitelist: drop unknown keys, never pass them through.
        params: dict = {}
        for key, value in resp.params.items():
            if key in schema:
                params[key] = value
            else:
                decisions.append({"rule": "G4", "decision": "strip", "field": f"params.{key}",
                                  "detail": "unknown param key dropped"})

        # G5 — param type/bounds (clamp) + instance_name resolution.
        for key, spec in schema.items():
            if key == "instance_name":
                continue  # resolved below (required; missing -> invalid_target)
            raw = params.get(key, spec.get("default"))
            present = key in params
            ptype = spec.get("type")
            if ptype == "bool":
                applied = _coerce_bool(raw, default=bool(spec.get("default", True)))
                if present and not isinstance(raw, bool):
                    decisions.append({"rule": "G5", "decision": "clamp",
                                      "field": f"params.{key}", "from": raw, "to": applied})
                params[key] = applied
            elif ptype == "int":
                applied = _coerce_int_clamped(raw, spec.get("min", TIMEOUT_S_MIN),
                                              spec.get("max", TIMEOUT_S_MAX))
                if applied is None:
                    applied = int(spec.get("default", 0))
                    if present:
                        decisions.append({"rule": "G5", "decision": "clamp",
                                          "field": f"params.{key}", "from": raw,
                                          "to": applied, "detail": "non-numeric -> default"})
                elif raw != applied:
                    decisions.append({"rule": "G5", "decision": "clamp",
                                      "field": f"params.{key}", "from": raw, "to": applied})
                params[key] = applied
            else:
                params[key] = raw

        # G5 — instance_name resolution (§5). A required-but-missing
        # instance_name (the adapter omits it, it does not invent one) is
        # unresolvable -> invalid_target.
        if "instance_name" in schema:
            raw_target = resp.params.get("instance_name")
            if raw_target is None:
                decisions.append({"rule": "G5", "decision": "reject",
                                  "field": "params.instance_name",
                                  "reason": "invalid_target",
                                  "detail": "required instance_name missing"})
                return GuardrailResult(ok=False, reject_reason="invalid_target", decisions=decisions)
            name, vm_id = _resolve_instance(
                raw_target, context.evidence, context.instances,
                context.instance_names or self.instance_names,
                context.default_instance_name or self.default_instance_name,
            )
            if name is None:
                decisions.append({"rule": "G5", "decision": "reject", "field": "params.instance_name",
                                  "reason": "invalid_target",
                                  "detail": f"instance target {raw_target!r} not resolvable"})
                return GuardrailResult(ok=False, reject_reason="invalid_target", decisions=decisions)
            params["instance_name"] = name
            if vm_id is not None:
                params["_vm_id"] = vm_id

        # G6 — confidence gate.
        confidence = float(resp.confidence)
        if confidence < self.min_confidence:
            decisions.append({"rule": "G6", "decision": "reject", "field": "confidence",
                              "reason": "low_confidence",
                              "detail": f"confidence {confidence} < min {self.min_confidence}"})
            return GuardrailResult(ok=False, reject_reason="low_confidence", decisions=decisions)

        # G7 — rationale sanitization (length + C0/C2 scan).
        rationale = resp.rationale if isinstance(resp.rationale, str) else ""
        if len(rationale) > RATIONALE_MAX_CHARS:
            decisions.append({"rule": "G7", "decision": "clamp", "field": "rationale",
                              "from": len(rationale), "to": RATIONALE_MAX_CHARS})
            rationale = rationale[:RATIONALE_MAX_CHARS]
        c2 = governance.scan_c2(rationale)
        if c2:
            decisions.append({"rule": "G7", "decision": "reject", "field": "rationale",
                              "reason": "schema_violation",
                              "detail": f"C2 signature in rationale: {c2[0]}"})
            return GuardrailResult(ok=False, reject_reason="schema_violation", decisions=decisions)

        # G8 — idempotency: deferred to the loop, which runs the action through
        # the existing idempotency gate (event_id-UNIQUE decision + 24 h window).
        decisions.append({"rule": "G8", "decision": "defer", "field": "idempotency",
                          "detail": "applied downstream by the loop (unchanged)"})

        # G9 — incident-record sanitization (§4.2). A title/explanation C2 hit is
        # a hard fallback (same rule as G7); a malformed/C2 entry is dropped and
        # recorded. The record itself never rejects the decision.
        incident, incident_reject = _sanitize_incident(resp, decisions)
        if incident_reject is not None:
            return GuardrailResult(ok=False, reject_reason=incident_reject,
                                   decisions=decisions)

        action = RemediationAction(
            kind=resp.action_id,
            params=params,
            reason=rationale,
            playbook=context.event.rule_id,
            rule_id=context.event.rule_id,
        )
        return GuardrailResult(ok=True, action=action, confidence=confidence,
                               reject_reason=None, decisions=decisions,
                               incident=incident)

    # ------------------------------------------------------------------ G1/G2
    @staticmethod
    def _coerce_response(response: Any) -> tuple[DecisionResponse | None, tuple[str, str] | None]:
        """Return ``(DecisionResponse, None)`` or ``(None, (reason, detail))``."""
        if isinstance(response, DecisionResponse):
            return response, None
        if isinstance(response, str):
            text = response.strip()
            if text.startswith("```"):
                text = text.strip("`")
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                return None, ("parse_error", f"model output is not valid JSON: {exc}")
            response = data
        if isinstance(response, dict):
            return Guardrail._response_from_dict(response)
        return None, ("schema_violation",
                      f"unexpected response type {type(response).__name__}")

    @staticmethod
    def _response_from_dict(d: dict) -> tuple[DecisionResponse | None, tuple[str, str] | None]:
        """Coerce a raw dict into a DecisionResponse (defensive; the adapter
        normally returns one already)."""
        try:
            schema_version = d.get("schema_version", SCHEMA_VERSION)
            if isinstance(schema_version, bool) or not isinstance(schema_version, int):
                raise ValueError("schema_version must be an int")
            action_id = d.get("action_id")
            if not isinstance(action_id, str) or not action_id.strip():
                raise ValueError("action_id must be a non-empty string")
            confidence = d.get("confidence")
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise ValueError("confidence must be a number")
            rationale = d.get("rationale")
            if not isinstance(rationale, str):
                rationale = ""
            params = d.get("params")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            request_id = d.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                request_id = None
            incident_title = d.get("incident_title")
            if not isinstance(incident_title, str):
                incident_title = ""
            explanation = d.get("explanation")
            if not isinstance(explanation, str):
                explanation = ""
            evidence = d.get("evidence")
            if not isinstance(evidence, list):
                evidence = []
            actions = d.get("actions")
            if not isinstance(actions, list):
                actions = []
            return DecisionResponse(
                action_id=action_id.strip(), confidence=float(confidence),
                rationale=rationale, params=params,
                schema_version=schema_version, request_id=request_id,
                incident_title=incident_title, explanation=explanation,
                evidence=evidence, actions=actions,
            ), None
        except (ValueError, TypeError) as exc:
            return None, ("schema_violation", str(exc))


# --------------------------------------------------------------------------- #
# Model provider (operator-decision.md §6, §8)
# --------------------------------------------------------------------------- #

# Guardrail evaluation order (G1..G9). Used by the trace's gates step to know
# whether G7/G9 were actually *reached* (a reject at G1..G6 returns early, so
# an absent G7/G9 entry then means "not evaluated", not "accepted").
_RULE_ORDER = {"G1": 1, "G2": 2, "G3": 3, "G4": 4, "G5": 5, "G6": 6,
               "G7": 7, "G8": 8, "G9": 9}


class ModelDecisionProvider:
    """Model-driven decision with the mandatory deterministic fallback (§8).

    The adapter call is injected via ``decide_fn`` for testability; when absent
    the ``termini.decision.decide`` adapter is imported lazily, so a missing or
    broken adapter degrades to ``inference_unavailable`` fallback rather than
    crashing the loop. The adapter's ``decide()`` never raises for transport /
    latency / parse problems — it returns a ``DecisionError`` whose ``reason`` is
    a §10.1 code, mapped straight onto the fallback.
    """

    def __init__(self, *, guardrail: Guardrail | None = None,
                 deterministic: DeterministicDecisionProvider | None = None,
                 decide_fn=None, timeout_s: float = DEFAULT_DECISION_TIMEOUT_S,
                 min_confidence: float = DEFAULT_DECISION_MIN_CONFIDENCE,
                 allowed_actions: Iterable[str] | None = None,
                 instance_names: dict | None = None,
                 default_instance_name: str = "agent",
                 audit_sink=None,
                 egress_probe=None,
                 egress_endpoint: str = ""):
        self.deterministic = deterministic
        self.decide_fn = decide_fn
        self.timeout_s = timeout_s
        # Optional sink for the `externalModelCall()` before/after audit records
        # (governance.md §9.3). None = audit to the log only.
        self.audit_sink = audit_sink
        # Optional callable returning the model endpoint's egress identity
        # (`app/operator/egress.py`). It labels WHERE the answer came from, so the
        # audit row can distinguish local weights from a forwarded managed model.
        # None = record the destination class derived from the URL alone.
        self.egress_probe = egress_probe
        # The endpoint URL the audit row names when the transport is injected
        # (`decide_fn`); production uses the adapter's own config.base_url.
        self.egress_endpoint = egress_endpoint
        self.guardrail = guardrail or Guardrail(
            min_confidence=min_confidence, allowed_actions=allowed_actions,
            instance_names=instance_names, default_instance_name=default_instance_name,
        )

    def decide(self, event: HealthEvent, context: DecisionContext) -> DecisionResult:
        trace = DecisionTrace(decided_by=TRACE_DECIDED_BY_MODEL)
        self._record_threshold(trace, event)
        self._record_evidence(trace, context)

        try:
            decision_input = build_decision_input(context)
        except Exception as exc:  # noqa: BLE001
            return self._fallback(context, "schema_violation",
                                  f"failed to build decision request: {exc}",
                                  trace=trace)

        try:
            response = self._call_model(decision_input, trace)
        except Exception as exc:  # noqa: BLE001 - a transport surprise must not escape
            return self._fallback(context, "inference_unavailable",
                                  f"{type(exc).__name__}: {exc}", trace=trace)

        # The adapter's decide() returns a DecisionError value (never raises) for
        # transport/latency/parse problems; map it straight onto the fallback.
        if isinstance(response, DecisionError):
            self._record_reply(trace, getattr(response, "raw", None),
                               state=TRACE_STATE_REJECTED, parse_outcome=response.reason)
            return self._fallback(context, response.reason, response.detail, trace=trace)
        if response is None:
            return self._fallback(context, "schema_violation",
                                  "adapter returned no decision response", trace=trace)

        result = self.guardrail.validate(response, context)
        self._record_reply(trace, getattr(response, "raw", None),
                           state=TRACE_STATE_OK)
        self._record_gates(trace, result.decisions)

        if not result.ok:
            return self._fallback(context, result.reject_reason or "schema_violation",
                                  "guardrail rejected model output",
                                  guardrail=result.decisions, trace=trace)

        self._record_trace_drops(trace)
        kind = result.action.kind if result.action is not None else "none"
        trace.add("action_decided", f"action decided: {kind}",
                  self._action_detail(result.action), state=TRACE_STATE_OK)

        return DecisionResult(action=result.action, source="model",
                              confidence=result.confidence, fallback_reason=None,
                              guardrail=list(result.decisions) + list(trace.drops),
                              incident=result.incident, trace=trace)

    # ------------------------------------------------------------ trace steps
    @staticmethod
    def _record_threshold(trace: DecisionTrace, event: HealthEvent) -> None:
        detect_lat = None
        try:
            from . import triggers as _triggers
            detect_lat = _triggers.detection_latency_s(event)
        except Exception:  # noqa: BLE001 — latency is best-effort, never fatal
            detect_lat = None
        detail = (f"rule_id={event.rule_id} event_id={event.event_id} "
                  f"fired_at={event.fired_at}")
        if detect_lat is not None:
            detail += f" detection_latency_ms={safe_int_display(detect_lat * 1000)}"
        trace.add("threshold_fired", f"rule {event.rule_id} fired", detail,
                  state=TRACE_STATE_OK)

    @staticmethod
    def _record_evidence(trace: DecisionTrace, context: DecisionContext) -> None:
        evidence = context.evidence or []
        seqs = []
        for sample in evidence:
            if isinstance(sample, dict):
                s = sample.get("seq")
                if isinstance(s, int) and not isinstance(s, bool):
                    seqs.append(s)
        parts = []
        if seqs:
            parts.append(f"seq {min(seqs)}..{max(seqs)} ({len(seqs)} samples)")
        state = context.state if isinstance(context.state, dict) else {}
        fields = _c0_field_names(state)
        if fields:
            parts.append("C0 fields: " + ", ".join(fields))
        trace.add("evidence_window", "evidence window captured",
                  " ".join(parts) or "no evidence window", state=TRACE_STATE_OK)

    @staticmethod
    def _record_reply(trace: DecisionTrace, raw: Any, *,
                      state: str, parse_outcome: str = "") -> None:
        label = ("model reply received" if not parse_outcome
                 else f"model reply (parse={parse_outcome})")
        if raw:
            trace.add_reply(label, raw, state=state)
        else:
            trace.add("reply_received", label, "no raw reply text retained",
                      state=TRACE_STATE_SKIPPED)

    @staticmethod
    def _record_gates(trace: DecisionTrace, decisions: list) -> None:
        if ModelDecisionProvider._reached(decisions, "G7"):
            g7 = [d for d in decisions if d.get("rule") == "G7"]
            if not g7:
                trace.add("gates", "G7 rationale",
                          "rule=G7 field=rationale decision=accept (C0-safe, within bounds)",
                          state=TRACE_STATE_OK)
            else:
                for d in g7:
                    trace.add("gates", "G7 rationale",
                              ModelDecisionProvider._gate_detail(d),
                              state=ModelDecisionProvider._gate_state(d))
        if ModelDecisionProvider._reached(decisions, "G9"):
            g9 = [d for d in decisions if d.get("rule") == "G9"]
            if not g9:
                trace.add("gates", "G9 incident record",
                          "rule=G9 decision=accept (record clean or absent)",
                          state=TRACE_STATE_OK)
            else:
                for d in g9:
                    trace.add("gates", "G9 incident record",
                              ModelDecisionProvider._gate_detail(d),
                              state=ModelDecisionProvider._gate_state(d))

    @staticmethod
    def _record_trace_drops(trace: DecisionTrace) -> None:
        for d in trace.drops:
            trace.add("gates", "G9 trace detail",
                      ModelDecisionProvider._gate_detail(d),
                      state=ModelDecisionProvider._gate_state(d))

    @staticmethod
    def _reached(decisions: list, rule: str) -> bool:
        if any(d.get("rule") == rule for d in decisions):
            return True
        for d in decisions:
            if d.get("decision") == "reject" and _RULE_ORDER.get(d.get("rule"), 99) < _RULE_ORDER[rule]:
                return False
        return True

    @staticmethod
    def _gate_detail(d: dict) -> str:
        out = f"rule={d.get('rule', '')} field={d.get('field', '')} decision={d.get('decision', '')}"
        reason = d.get("reason", "")
        if reason:
            out += f" reason={reason}"
        detail = d.get("detail", "")
        if detail:
            out += f" ({detail})"
        return out

    @staticmethod
    def _gate_state(d: dict) -> str:
        decision = d.get("decision", "")
        return {
            "accept": TRACE_STATE_OK,
            "clamp": TRACE_STATE_CLAMPED,
            "drop": TRACE_STATE_DROPPED,
            "reject": TRACE_STATE_REJECTED,
            "defer": TRACE_STATE_SKIPPED,
        }.get(decision, TRACE_STATE_OK)

    @staticmethod
    def _rejecting_gate(guardrail: list | None) -> str | None:
        for d in (guardrail or []):
            if d.get("decision") == "reject":
                return d.get("rule")
        return None

    @staticmethod
    def _action_detail(action: RemediationAction | None) -> str:
        target = ""
        if action is not None and isinstance(getattr(action, "params", None), dict):
            target = str(action.params.get("instance_name") or "").strip()
        kind = action.kind if action is not None else "none"
        return f"action_kind={kind} target={target or '-'}"

    @staticmethod
    def _prompt_built_detail(before: dict | None, *, failed: bool = False,
                             violations: list | None = None) -> str:
        if not isinstance(before, dict):
            before = {}
        parts = [
            f"template={before.get('template_version', '')}",
            f"sha256={sha256_display(before.get('payload_sha256', ''))}",
            f"substituted_fields={safe_int_display(before.get('substituted_fields', 0))}",
        ]
        if failed:
            parts.append("C0_assertion=FAILED")
            if violations:
                shown = " | ".join(str(v) for v in violations[:5])
                parts.append(f"violations={shown}")
        else:
            parts.append("C0_assertion=ok")
        return " ".join(parts)

    @staticmethod
    def _model_call_detail(before: dict | None, latency_ms: float, *,
                           result: Any = None, error: str | None = None) -> str:
        if not isinstance(before, dict):
            before = {}
        egress = before.get("egress")
        if not isinstance(egress, dict):
            egress = {}
        dest_class = before.get("destination_class", "") or ""
        endpoint_class = {"in-vpc": "local", "external": "external"}.get(
            dest_class, dest_class or "unknown")
        answered_by = egress.get("answered_by") or "unknown"
        model_id = egress.get("model") or egress.get("alias") or ""
        parts = [
            f"endpoint={endpoint_class}",
            f"model={model_id or 'unknown'}",
            f"answered_by={answered_by}",
            f"latency_ms={safe_int_display(latency_ms)}",
        ]
        usage = getattr(result, "usage", None) if result is not None else None
        if isinstance(usage, dict) and usage:
            pt = safe_int_display(usage.get("prompt_tokens"))
            ct = safe_int_display(usage.get("completion_tokens"))
            tt = safe_int_display(usage.get("total_tokens"))
            parts.append(f"tokens(prompt={pt},completion={ct},total={tt})")
        rid = before.get("request_id", "") or ""
        if rid:
            parts.append(f"request_id={rid}")
        if error:
            parts.append(f"error={error}")
        return " ".join(parts)

    # ------------------------------------------------------------------ model
    def _call_model(self, decision_input: DecisionInput, trace: DecisionTrace):
        """Call the model through the guard — ALWAYS, whichever transport is used.

        The `decide_fn`/adapter choice is only about *transport*: the §7.2 class
        assertion, the before/after audit row and the egress labelling must apply
        to both, or the audit would be bypassable by construction (the defect the
        wiring test in `test_external_model_call.py` exists to catch). The
        `prompt_built` and `model_call` trace steps are recorded here from the
        real `externalModelCall()` before/after audit records — never from a
        parallel re-derivation.
        """
        if self.decide_fn is not None:
            transport = lambda: self.decide_fn(decision_input)  # noqa: E731
            config = None
        else:
            # The adapter is imported lazily so deterministic mode never touches it.
            try:
                from termini.decision import DecisionModelConfig
                from termini.decision import decide as adapter_decide
            except ImportError as exc:
                return DecisionError(
                    "inference_unavailable", f"decision adapter unavailable: {exc}"
                )
            config = DecisionModelConfig.from_env().with_timeout(self.timeout_s)
            transport = lambda: adapter_decide(decision_input, config=config)  # noqa: E731

        # governance.md §7.2/§9.3 (controller-api.md §117): every model call is
        # wrapped by `externalModelCall()`, which asserts the frozen template
        # version and the C-class of every substituted field BEFORE the call. A
        # non-conforming payload is never sent — it becomes a `schema_violation`
        # and the loop falls back deterministically (fail-closed).
        #
        # Destination-agnostic on purpose: the guard applies identically to the
        # external Cloud Run model and to llama.cpp on node C. Hosting the model
        # on-prem narrows the blast radius of an egress; it does not repeal the
        # boundary (governance.md §7 makes no exception for an on-prem endpoint).
        from . import external_call

        base_url = config.base_url if config is not None else (self.egress_endpoint or "")

        # What the endpoint says it is (local weights vs a forwarded managed
        # model). Probed before the call so the BEFORE audit record is labelled
        # too; a probe failure is recorded as unknown, never as "local".
        egress = None
        if self.egress_probe is not None:
            try:
                egress = self.egress_probe()
            except Exception as exc:  # noqa: BLE001 - labelling must not break the call
                log.warning("egress probe failed (recording as unknown): %s", exc)
                egress = None

        audit_records: list = []

        def _sink(record):
            audit_records.append(record)
            if self.audit_sink is not None:
                self.audit_sink(record)

        t0 = time.monotonic()
        try:
            result = external_call.external_model_call(
                decision_input,
                send=transport,
                destination=base_url,
                request_id=decision_input.request_id,
                audit_sink=_sink,
                egress=egress,
            )
        except external_call.ExternalCallViolation as exc:
            before = audit_records[0] if audit_records else {}
            trace.add("prompt_built", "prompt rendered, C0 assertion FAILED",
                      self._prompt_built_detail(before, failed=True,
                                                violations=exc.violations),
                      state=TRACE_STATE_REJECTED)
            log.warning("externalModelCall rejected the decision payload: %s", exc)
            return DecisionError("schema_violation", str(exc))
        except Exception as exc:  # noqa: BLE001 — mapped onto fallback by decide()
            before = audit_records[0] if audit_records else {}
            latency = round((time.monotonic() - t0) * 1000, 1)
            if before:
                trace.add("prompt_built", "prompt rendered (C0 assertion ok)",
                          self._prompt_built_detail(before), state=TRACE_STATE_OK)
            trace.add("model_call", "model call FAILED",
                      self._model_call_detail(before, latency, error=str(exc)),
                      state=TRACE_STATE_REJECTED)
            raise

        latency = round((time.monotonic() - t0) * 1000, 1)
        before = audit_records[0] if audit_records else {}
        trace.add("prompt_built", "prompt rendered (C0 assertion ok)",
                  self._prompt_built_detail(before), state=TRACE_STATE_OK)
        trace.add("model_call", "model answered",
                  self._model_call_detail(before, latency, result=result),
                  state=TRACE_STATE_OK)
        return result

    # --------------------------------------------------------------- fallback
    def _fallback(self, context: DecisionContext, reason: str, detail: str,
                  guardrail: list | None = None, trace: DecisionTrace | None = None) -> DecisionResult:
        if reason not in FALLBACK_REASONS:
            reason = "inference_unavailable"
        det = self.deterministic or DeterministicDecisionProvider(
            playbooks={}, instance_names=context.instance_names,
            default_instance_name=context.default_instance_name,
        )
        base = det.decide(context.event, context)
        log.warning("model decision fell back (reason=%s): %s", reason, detail)
        incident = build_fallback_incident(context.event, base.action, reason)
        if trace is None:
            trace = DecisionTrace(decided_by=TRACE_DECIDED_BY_FALLBACK)
        trace.decided_by = TRACE_DECIDED_BY_FALLBACK
        self._record_trace_drops(trace)
        rejecting = self._rejecting_gate(guardrail)
        action_kind = base.action.kind if base.action is not None else "none"
        trace.add("action_decided", "fallback to deterministic action",
                  f"decided_by=fallback reason={reason} action_kind={action_kind}"
                  + (f" rejecting_gate={rejecting}" if rejecting else ""),
                  state=TRACE_STATE_FALLBACK)
        merged = list(guardrail or []) + list(trace.drops)
        return DecisionResult(action=base.action, source="fallback",
                              confidence=None, fallback_reason=reason,
                              guardrail=merged, incident=incident, trace=trace)


# --------------------------------------------------------------------------- #
# Fallback-streak transition (operator-decision.md §10.2) — pure.
# --------------------------------------------------------------------------- #

def transition_fallback_state(state: dict | None, outcome: str, now_iso: str,
                              max_fallbacks: int, cooldown_s: float) -> tuple[dict, str | None]:
    """Advance the per-(node, rule) fallback streak.

    ``outcome`` is ``"success"`` (a ``source="model"`` decision) or
    ``"fallback"``. Returns ``(new_state, forced_until)`` where ``new_state`` is
    ``{"fallback_streak": int, "forced_until": str | None}`` and ``forced_until``
    is set when the streak cap is reached (forcing a deterministic cooldown).
    """
    streak = int((state or {}).get("fallback_streak", 0) or 0)
    forced_until = (state or {}).get("forced_until")

    if outcome == "success":
        return {"fallback_streak": 0, "forced_until": None}, None

    streak += 1
    if streak >= max_fallbacks:
        forced_until = _iso_add_seconds(now_iso, cooldown_s)
        return {"fallback_streak": 0, "forced_until": forced_until}, forced_until
    return {"fallback_streak": streak, "forced_until": forced_until}, None


def in_forced_cooldown(state: dict | None, now_iso: str) -> bool:
    """True while ``now < forced_until`` for this (node, rule)."""
    forced = (state or {}).get("forced_until")
    if not forced:
        return False
    return now_iso < forced  # both RFC3339 ms; lexicographic == chronological


def _iso_add_seconds(now_iso: str, seconds: float) -> str:
    import datetime
    from . import triggers as _triggers
    epoch = _triggers.parse_epoch_ms(now_iso)
    if epoch is None:
        epoch = 0.0
    dt = datetime.datetime.fromtimestamp(epoch + seconds, datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# --------------------------------------------------------------------------- #
# F05 restart-refusal guard — fence the workload off a read-only disk (§7)
# --------------------------------------------------------------------------- #
# The counterweight to the F01 restart: when a disk remounts read-only, the
# right move is to STOP writes and then refuse to put the workload back. This
# guard is the "refuse" half, and it is guardrail-level, not prompt-level: the
# loop and the standalone CLI apply it to the node's latest telemetry state
# regardless of what the model or the playbook decided, so a `restart`/`start`
# that would resume writes onto the failed disk never reaches the executor.

# The machine-readable refusal reason (also written to the audit row's `reason`).
DISK_FENCED = "disk_fenced"

# Action kinds that would bring a workload back onto a node (a restart, or a
# start of a stopped instance): refused while the node's latest telemetry still
# shows a read-only disk mount.
FENCED_ACTION_KINDS = ("instance.restart", "instance.start")


def read_only_mounts(state: Any) -> list[str]:
    """`path`s of the disk entries whose mount is read-only in `state`.

    `state` is the node's latest telemetry sample (the C0 `DecisionContext.state`
    the loop already carries). Returns `[]` when nothing is read-only.
    """
    if not isinstance(state, dict):
        return []
    out: list[str] = []
    for entry in state.get("disk") or []:
        if isinstance(entry, dict) and entry.get("mount_ro") is True:
            out.append(str(entry.get("path") or entry.get("mount") or "unknown"))
    return out


def fenced_action_refusal(state: Any, action_kind: str) -> str | None:
    """Return `DISK_FENCED` when `action_kind` would (re)start a workload on a
    node whose latest telemetry still shows a read-only disk mount, else None.

    This is the F05 restart-refusal guard: once a disk has remounted read-only,
    the workload is fenced and must NOT be brought back — a restart/start would
    resume writes onto the failing disk. A stop/delete/prune is always allowed
    (stopping writes is the correct half of the response).
    """
    if action_kind not in FENCED_ACTION_KINDS:
        return None
    if not isinstance(state, dict):
        return None
    if detect.any_disk_mount_ro(state):
        return DISK_FENCED
    return None


def fenced_refusal_detail(state: Any, action_kind: str, node_id: str) -> str:
    """Human-readable explanation for a refused (re)start: names the mount(s)."""
    mounts = ", ".join(read_only_mounts(state)) or "an unreported mount"
    return (
        f"{DISK_FENCED}: refused {action_kind} on node {node_id} — the node's "
        f"latest telemetry has a disk remounted read-only (mount_ro=true on "
        f"{mounts}); the workload is fenced and must not be brought back until "
        f"a human replaces the disk and raises the instance elsewhere"
    )


# --------------------------------------------------------------------------- #
# Provider factory (operator-decision.md §14.1 — main.py)
# --------------------------------------------------------------------------- #

def build_decision_provider(cfg, *, playbooks: dict | None = None,
                            instance_names: dict | None = None,
                            default_instance_name: str = "agent") -> DecisionProvider:
    """Construct the provider for the configured decision mode.

    ``deterministic`` (default): only the deterministic provider is built — no
    model-call code path is reachable. ``model``: a ``ModelDecisionProvider``
    whose deterministic fallback is injected by the loop.
    """
    mode = (getattr(cfg, "operator_decision_mode", "deterministic") or "deterministic")
    if mode == "model":
        return ModelDecisionProvider(
            min_confidence=float(getattr(cfg, "operator_decision_min_confidence",
                                         DEFAULT_DECISION_MIN_CONFIDENCE)),
            timeout_s=float(getattr(cfg, "operator_decision_timeout_s",
                                    DEFAULT_DECISION_TIMEOUT_S)),
            allowed_actions=ACTION_IDS,
            instance_names=instance_names,
            default_instance_name=default_instance_name,
        )
    return DeterministicDecisionProvider(
        playbooks=playbooks or {}, instance_names=instance_names,
        default_instance_name=default_instance_name,
    )


__all__ = [
    "ACTION_IDS",
    "DISK_FENCED",
    "DecisionContext",
    "DecisionProvider",
    "DecisionResult",
    "DecisionTrace",
    "DeterministicDecisionProvider",
    "FENCED_ACTION_KINDS",
    "Guardrail",
    "GuardrailResult",
    "ModelDecisionProvider",
    "FALLBACK_REASONS",
    "build_allowed_action_space",
    "build_decision_input",
    "build_decision_provider",
    "build_fallback_incident",
    "build_window_deltas",
    "fenced_action_refusal",
    "fenced_refusal_detail",
    "in_forced_cooldown",
    "read_only_mounts",
    "safe_int_display",
    "sha256_display",
    "transition_fallback_state",
]
