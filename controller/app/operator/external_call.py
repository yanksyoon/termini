"""``externalModelCall()`` — the controller-side external-call boundary wrapper.

Normative sources
-----------------
* ``docs/contracts/governance.md`` §7.2 — "The controller wraps every model call
  with ``externalModelCall()`` which asserts the template version and the class of
  every substituted field (all C0) before sending."
* ``docs/contracts/governance.md`` §9.3 — the hook is listed among the controller
  enforcement hooks that "MUST implement these as code, not policy prose".
* ``docs/contracts/controller-api.md`` — ``externalModelCall()`` is named twice as a
  frozen hook the API must expose.

Why this module exists
----------------------
Before this module the hook existed **only in the contract text**: a whole-tree
search found ``externalModelCall`` in ``.md`` files and nowhere under
``controller/app/``. Meanwhile ``build_decision_input`` passes
``state=dict(context.state)`` and ``evidence=list(context.evidence)`` — the stored
telemetry samples — *wholesale* into the prompt with no class assertion at all.
So "no C2 content reaches the model" was an assertion, not an enforceable
property. This module is the enforcement point.

What it asserts (fail-closed)
-----------------------------
``assert_allowed_classes()`` walks the request payload that is actually
substituted into the prompt and returns a violation for:

1. any field **name** that denotes C2 (``governance.is_c2_field_name``);
2. any field that is **not on the C0/C1 telemetry allowlist** — an unknown key is
   not *provably* C0, so it is refused rather than forwarded;
3. a C1 identifier that is present in **untransformed** form (raw hostname / MAC /
   local IP / project id), reusing ``governance.check_c1_transforms``;
4. any C2 **value signature** (email, phone, raw IP/MAC, key material, card, …),
   reusing ``governance.scan_c2``.

The caller decides what a violation means; the decision loop treats one as a
``schema_violation`` and falls back to the deterministic provider, so a
non-conforming payload is **never sent** (fail-closed, per ``governance.md`` §6).

The guard is destination-agnostic on purpose: it applies identically whether the
model is the external Cloud Run service or llama.cpp on node C. Moving the model
on-prem narrows the *blast radius* of an egress, it does not repeal the boundary
(``governance.md`` §7 makes no exception for an on-prem endpoint).
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .. import fields as F
from .. import governance

log = logging.getLogger("termini.operator.external_call")

# The frozen prompt-template id (§7.2: "a versioned template").
# Imported lazily from termini.decision.prompt to avoid a hard import cycle; the
# literal here is the fallback used when the package is unavailable.
_FALLBACK_TEMPLATE_VERSION = "operator-decision v1"

# The DecisionInput top-level keys (§3 wire shape). Anything else at the top
# level is not part of the frozen request and is refused.
_DECISION_INPUT_KEYS = {
    "schema_version", "request_id", "project_id", "node_id", "rule_id", "type",
    "trigger", "state", "evidence", "target_resource", "allowed_action_space",
}

# The trigger sub-object keys (§3).
_TRIGGER_KEYS = {
    "metric", "value", "threshold", "fired_at", "breach_started_at", "episode",
}

# The target_resource sub-object keys (§3).
_TARGET_RESOURCE_KEYS = {"node_id", "instances"}

# The allowed_action_space entry keys (§3/§5).
_ACTION_SPACE_ENTRY_KEYS = {"action_id", "params_schema"}


def template_version() -> str:
    """The frozen prompt-template version (``operator-decision v1``)."""
    try:
        from termini.decision.prompt import TEMPLATE_VERSION  # noqa: WPS433
        return TEMPLATE_VERSION
    except Exception:  # noqa: BLE001 — the literal is the contract value
        return _FALLBACK_TEMPLATE_VERSION


# --------------------------------------------------------------------------- #
# Class assertion (§7.2)
# --------------------------------------------------------------------------- #

def _violations_for_sample(path: str, sample: Any, out: List[str]) -> None:
    """Assert one telemetry sample is C0 / transformed-C1 only."""
    if not isinstance(sample, dict):
        return
    for key, value in sample.items():
        if not isinstance(key, str):
            out.append(f"{path}: non-string field name {key!r}")
            continue
        if governance.is_c2_field_name(key):
            out.append(f"{path}.{key}: C2 field name")
            continue
        if key in F.SAMPLE_FIELDS:
            # A known scalar. Its schema `cclass` is the authority for its class.
            continue
        if key in F.SAMPLE_CONTAINERS:
            container = F.SAMPLE_CONTAINERS[key]
            sub_fields = container.get("fields") or {}
            if container["kind"] == "list":
                if not isinstance(value, list):
                    continue
                for idx, item in enumerate(value):
                    if not isinstance(item, dict):
                        out.append(f"{path}.{key}[{idx}]: not an object")
                        continue
                    for sub_key in item:
                        if sub_key not in sub_fields:
                            out.append(
                                f"{path}.{key}[{idx}].{sub_key}: not on the C0 "
                                f"allowlist (not provably C0)"
                            )
            else:  # obj
                if isinstance(value, dict):
                    for sub_key in value:
                        if sub_key not in sub_fields:
                            out.append(
                                f"{path}.{key}.{sub_key}: not on the C0 allowlist "
                                f"(not provably C0)"
                            )
            continue
        out.append(f"{path}.{key}: field not on the C0/C1 telemetry allowlist "
                   f"(not provably C0)")


def _violations_for_instances(path: str, instances: Any, out: List[str]) -> None:
    """Assert ``target_resource.instances[]`` (C0) has only known keys."""
    if not isinstance(instances, list):
        out.append(f"{path}: expected a list of instances")
        return
    for idx, inst in enumerate(instances):
        if not isinstance(inst, dict):
            out.append(f"{path}[{idx}]: not an object")
            continue
        for key in inst:
            if key not in F.LXD_INSTANCE_FIELDS:
                out.append(f"{path}[{idx}].{key}: not a C0 instance field")


def assert_allowed_classes(payload: Any) -> List[str]:
    """Return the §7.2 class violations in a decision-request payload.

    An **empty list** means every substituted field is C0 or a transformed C1
    identifier. The check is deliberately deny-by-default: an unknown field name
    is a violation, not a pass, because "not on the allowlist" is not the same as
    "known to be C0".
    """
    violations: List[str] = []
    if not isinstance(payload, dict):
        return [f"decision payload is {type(payload).__name__}, expected an object"]

    for key in payload:
        if key not in _DECISION_INPUT_KEYS:
            violations.append(f"{key}: not a frozen DecisionInput field")

    trigger = payload.get("trigger")
    if isinstance(trigger, dict):
        for key in trigger:
            if key not in _TRIGGER_KEYS:
                violations.append(f"trigger.{key}: not a frozen trigger field")

    for label, value in (("state", payload.get("state")),):
        if value is not None:
            _violations_for_sample(label, value, violations)

    evidence = payload.get("evidence")
    if isinstance(evidence, list):
        for idx, sample in enumerate(evidence):
            _violations_for_sample(f"evidence[{idx}]", sample, violations)
    elif evidence is not None:
        violations.append("evidence: expected a list of samples")

    target = payload.get("target_resource")
    if isinstance(target, dict):
        for key in target:
            if key not in _TARGET_RESOURCE_KEYS:
                violations.append(f"target_resource.{key}: not a frozen field")
        if "instances" in target:
            _violations_for_instances(
                "target_resource.instances", target.get("instances"), violations
            )

    space = payload.get("allowed_action_space")
    if isinstance(space, list):
        for idx, entry in enumerate(space):
            if isinstance(entry, dict):
                for key in entry:
                    if key not in _ACTION_SPACE_ENTRY_KEYS:
                        violations.append(
                            f"allowed_action_space[{idx}].{key}: not a frozen field"
                        )
    elif space is not None:
        violations.append("allowed_action_space: expected a list")

    # Value-signature scan + C1 transform check over the whole payload: this
    # catches C2 *carried in an allowed field* (governance.md §6 fail-closed).
    for sample in _sample_iter(payload):
        violations.extend(f"state/evidence: {v}" for v in governance.scan_c2(sample))
        violations.extend(
            f"state/evidence: {v}" for v in governance.check_c1_transforms(sample)
        )

    return violations


def _sample_iter(payload: Dict[str, Any]):
    state = payload.get("state")
    if isinstance(state, dict):
        yield state
    evidence = payload.get("evidence")
    if isinstance(evidence, list):
        for sample in evidence:
            if isinstance(sample, dict):
                yield sample


# --------------------------------------------------------------------------- #
# The wrapper (§9.3: assert, audit before, call, audit after)
# --------------------------------------------------------------------------- #

@dataclass
class ExternalCallAudit:
    """One audit record for an ``externalModelCall()`` invocation (§8.2/§9.3)."""

    request_id: str
    template_version: str
    destination: str
    phase: str                       # "before" | "after"
    outcome: str                     # "ok" | "returned" | "error" | "rejected"
    payload_sha256: str = ""
    substituted_fields: int = 0
    detail: str = ""
    violations: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "template_version": self.template_version,
            "destination": self.destination,
            "phase": self.phase,
            "outcome": self.outcome,
            "payload_sha256": self.payload_sha256,
            "substituted_fields": self.substituted_fields,
            "detail": self.detail,
            "violations": list(self.violations),
        }


def _count_fields(payload: Any) -> int:
    """Count substituted leaves in the payload (for the audit record)."""
    if isinstance(payload, dict):
        return sum(1 + _count_fields(v) for v in payload.values())
    if isinstance(payload, list):
        return sum(_count_fields(v) for v in payload)
    return 1


def _sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


class ExternalCallViolation(Exception):
    """A decision payload failed the §7.2 class assertion; nothing was sent."""

    def __init__(self, violations: List[str]):
        self.violations = list(violations)
        super().__init__(
            "externalModelCall class assertion failed: " + "; ".join(self.violations)
        )


def external_model_call(
    payload: Any,
    send: Callable[[], Any],
    *,
    template_version_expected: Optional[str] = None,
    template_version_actual: Optional[str] = None,
    destination: str = "",
    request_id: str = "",
    audit_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
):
    """Assert, audit-before, call, audit-after — the §9.3 ``externalModelCall()``.

    ``payload`` is the request object (a :class:`DecisionInput` or its ``dict``
    form) whose fields are substituted into the prompt. ``send`` performs the
    actual transport and is called **only** when the assertion passes.

    Returns whatever ``send`` returns. On a class violation it raises
    :class:`ExternalCallViolation` *without* calling ``send`` — the caller maps
    that onto the deterministic fallback (fail-closed). A template-version
    mismatch is a violation of the same guard.
    """
    expected = template_version_expected or template_version()
    actual = template_version_actual or expected

    if isinstance(payload, dict):
        payload_dict = payload
    else:
        payload_dict = payload.to_dict() if hasattr(payload, "to_dict") else payload
    if not request_id and isinstance(payload_dict, dict):
        request_id = str(payload_dict.get("request_id") or "")

    violations: List[str] = []
    if actual != expected:
        violations.append(
            f"template version {actual!r} != frozen {expected!r} (governance §7.2)"
        )
    violations.extend(assert_allowed_classes(payload_dict))

    digest = _sha256(payload_dict) if isinstance(payload_dict, (dict, list)) else ""
    n_fields = _count_fields(payload_dict)

    def _audit(phase: str, outcome: str, detail: str = "",
               viol: Optional[List[str]] = None) -> None:
        record = ExternalCallAudit(
            request_id=request_id, template_version=actual, destination=destination,
            phase=phase, outcome=outcome, payload_sha256=digest,
            substituted_fields=n_fields, detail=detail, violations=viol or [],
        )
        log.info("externalModelCall %s: %s", phase, record.as_dict())
        if audit_sink is not None:
            try:
                audit_sink(record.as_dict())
            except Exception:  # noqa: BLE001 — auditing must not break the loop
                log.exception("externalModelCall audit sink failed")

    _audit("before", "rejected" if violations else "ok", viol=violations)
    if violations:
        raise ExternalCallViolation(violations)

    try:
        result = send()
    except Exception as exc:  # noqa: BLE001
        _audit("after", "error", detail=f"{type(exc).__name__}: {exc}")
        raise
    _audit("after", "returned", detail=type(result).__name__)
    return result


__all__ = [
    "ExternalCallAudit",
    "ExternalCallViolation",
    "assert_allowed_classes",
    "external_model_call",
    "template_version",
]
