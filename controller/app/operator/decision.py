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
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, runtime_checkable

from .. import ulid
from .. import governance
from .models import HealthEvent, RemediationAction
from . import playbook

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


@runtime_checkable
class DecisionProvider(Protocol):
    def decide(self, event: HealthEvent, context: DecisionContext) -> DecisionResult:
        ...


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
        schema_version=SCHEMA_VERSION,
    )


# --------------------------------------------------------------------------- #
# Deterministic provider (operator-decision.md §8)
# --------------------------------------------------------------------------- #

class DeterministicDecisionProvider:
    """``playbook.choose`` with ``source="deterministic"``, ``confidence=None``.

    Byte-identical to today's behavior so existing operator-loop tests pass
    unchanged in deterministic mode.
    """

    def __init__(self, *, playbooks: dict, instance_names: dict | None = None,
                 default_instance_name: str = "agent"):
        self.playbooks = playbooks
        self.instance_names = instance_names or {}
        self.default_instance_name = default_instance_name

    def decide(self, event: HealthEvent, context: DecisionContext | None = None) -> DecisionResult:
        action = playbook.choose(
            event, self.playbooks, self.instance_names, self.default_instance_name
        )
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

        action = RemediationAction(
            kind=resp.action_id,
            params=params,
            reason=rationale,
            playbook=context.event.rule_id,
            rule_id=context.event.rule_id,
        )
        return GuardrailResult(ok=True, action=action, confidence=confidence,
                               reject_reason=None, decisions=decisions)

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
            return DecisionResponse(
                action_id=action_id.strip(), confidence=float(confidence),
                rationale=rationale, params=params,
                schema_version=schema_version, request_id=request_id,
            ), None
        except (ValueError, TypeError) as exc:
            return None, ("schema_violation", str(exc))


# --------------------------------------------------------------------------- #
# Model provider (operator-decision.md §6, §8)
# --------------------------------------------------------------------------- #

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
                 audit_sink=None):
        self.deterministic = deterministic
        self.decide_fn = decide_fn
        self.timeout_s = timeout_s
        # Optional sink for the `externalModelCall()` before/after audit records
        # (governance.md §9.3). None = audit to the log only.
        self.audit_sink = audit_sink
        self.guardrail = guardrail or Guardrail(
            min_confidence=min_confidence, allowed_actions=allowed_actions,
            instance_names=instance_names, default_instance_name=default_instance_name,
        )

    def decide(self, event: HealthEvent, context: DecisionContext) -> DecisionResult:
        try:
            decision_input = build_decision_input(context)
        except Exception as exc:  # noqa: BLE001
            return self._fallback(context, "schema_violation",
                                  f"failed to build decision request: {exc}")

        try:
            response = self._call_model(decision_input)
        except Exception as exc:  # noqa: BLE001 - a transport surprise must not escape
            return self._fallback(context, "inference_unavailable",
                                  f"{type(exc).__name__}: {exc}")

        # The adapter's decide() returns a DecisionError value (never raises) for
        # transport/latency/parse problems; map it straight onto the fallback.
        if isinstance(response, DecisionError):
            return self._fallback(context, response.reason, response.detail)
        if response is None:
            return self._fallback(context, "schema_violation",
                                  "adapter returned no decision response")

        result = self.guardrail.validate(response, context)
        if not result.ok:
            return self._fallback(context, result.reject_reason or "schema_violation",
                                  "guardrail rejected model output",
                                  guardrail=result.decisions)

        return DecisionResult(action=result.action, source="model",
                              confidence=result.confidence, fallback_reason=None,
                              guardrail=result.decisions)

    # ------------------------------------------------------------------ model
    def _call_model(self, decision_input: DecisionInput):
        if self.decide_fn is not None:
            return self.decide_fn(decision_input)
        # The adapter is imported lazily so deterministic mode never touches it.
        try:
            from termini.decision import DecisionModelConfig
            from termini.decision import decide as adapter_decide
        except ImportError as exc:
            return DecisionError(
                "inference_unavailable", f"decision adapter unavailable: {exc}"
            )
        config = DecisionModelConfig.from_env().with_timeout(self.timeout_s)
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

        try:
            return external_call.external_model_call(
                decision_input,
                send=lambda: adapter_decide(decision_input, config=config),
                destination=config.base_url,
                request_id=decision_input.request_id,
                audit_sink=self.audit_sink,
            )
        except external_call.ExternalCallViolation as exc:
            log.warning("externalModelCall rejected the decision payload: %s", exc)
            return DecisionError("schema_violation", str(exc))

    # --------------------------------------------------------------- fallback
    def _fallback(self, context: DecisionContext, reason: str, detail: str,
                  guardrail: list | None = None) -> DecisionResult:
        if reason not in FALLBACK_REASONS:
            reason = "inference_unavailable"
        det = self.deterministic or DeterministicDecisionProvider(
            playbooks={}, instance_names=context.instance_names,
            default_instance_name=context.default_instance_name,
        )
        base = det.decide(context.event, context)
        log.warning("model decision fell back (reason=%s): %s", reason, detail)
        return DecisionResult(action=base.action, source="fallback",
                              confidence=None, fallback_reason=reason,
                              guardrail=guardrail or [])


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
    "DecisionContext",
    "DecisionProvider",
    "DecisionResult",
    "DeterministicDecisionProvider",
    "Guardrail",
    "GuardrailResult",
    "ModelDecisionProvider",
    "FALLBACK_REASONS",
    "build_allowed_action_space",
    "build_decision_input",
    "build_decision_provider",
    "in_forced_cooldown",
    "transition_fallback_state",
]
