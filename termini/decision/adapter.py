"""Model-driven operator decision adapter (inference side).

Turns a C0 :class:`~termini.decision.types.DecisionInput` into a model request,
calls the existing on-prem llama.cpp endpoint (``docs/contracts/operator-decision.md``
§6.1 — the same surface ``termini.intent`` uses), and parses the completion into
a well-shaped :class:`~termini.decision.types.DecisionResponse` or a typed
:class:`~termini.decision.types.DecisionError`.

Boundary (contract §6.2): this adapter does **parse + light repair only**. It
guarantees the output is *well-shaped* — valid JSON, an object, an allowed-named
action, numeric confidence, a bounded rationale, and only known param keys. It
does **not** decide whether an action is *authorized*: the whitelist (G3), the
confidence gate (G6), the target resolution (G5), and the C2 scan (G7) are the
controller-side guardrail's job (``controller/app/operator/decision.py``). The
adapter never fabricates an action — anything it cannot repair into a
well-shaped response becomes a :class:`DecisionError` so the caller can fall
back to the deterministic provider.
"""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Union

from . import prompt as prompt_module
from .config import REQUEST_ID_HEADER, DecisionModelConfig
from .types import (
    ACTION_NOTE_MAX_CHARS,
    ACTION_SPACE,
    ACTION_TARGET_MAX_CHARS,
    ACTIONS_MAX_ENTRIES,
    EVIDENCE_FIELD_MAX_CHARS,
    EVIDENCE_FIELD_PATTERN,
    EVIDENCE_MAX_ENTRIES,
    EVIDENCE_VALUE_STR_MAX_CHARS,
    EXPLANATION_MAX_CHARS,
    INCIDENT_TITLE_MAX_CHARS,
    RATIONALE_MAX_CHARS,
    RATIONALE_MIN_CHARS,
    REASON_INFERENCE_TIMEOUT,
    REASON_INFERENCE_UNAVAILABLE,
    REASON_PARSE_ERROR,
    REASON_SCHEMA_VIOLATION,
    SCHEMA_VERSION,
    DecisionError,
    DecisionInput,
    DecisionResponse,
)

# A model client receives the built messages (and the correlation id) and
# returns the assistant's text content (a JSON string). Injectable so tests
# never touch the network — mirrors ``termini.intent.extractor.ModelClient``.
ModelClient = Callable[..., str]

# How much of the raw completion to retain on a DecisionError for debugging.
_RAW_CLIP = 2048


class DecisionTransportError(Exception):
    """The inference endpoint returned an unusable response envelope."""


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(decision_input: Union[DecisionInput, Dict[str, Any]]) -> List[Dict[str, str]]:
    """Render a :class:`DecisionInput` into the model message list (§6.1)."""
    return prompt_module.build_messages(decision_input)


# ---------------------------------------------------------------------------
# Default transport (the existing on-prem endpoint — no new service)
# ---------------------------------------------------------------------------

def default_client(config: DecisionModelConfig) -> ModelClient:
    """Build an HTTP client against the on-prem OpenAI-compatible router.

    ``response_format: json_object`` is set so the completion parses
    deterministically and is never free-form (§6.1).
    """

    def _call(messages: List[Dict[str, str]], request_id: Optional[str] = None, **kwargs: Any) -> str:
        body = {
            "model": config.model,
            "messages": messages,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if config.token:
            headers["Authorization"] = "Bearer " + config.token
        if request_id:
            headers[REQUEST_ID_HEADER] = request_id
        req = urllib.request.Request(
            config.chat_url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=config.timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DecisionTransportError(f"unexpected model response shape: {exc}") from exc
        if not isinstance(content, str):
            raise DecisionTransportError(
                f"model content is {type(content).__name__}, expected a string"
            )
        return content

    return _call


# ---------------------------------------------------------------------------
# Parse + light repair (contract §6.2, steps 1–7)
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    """Step 1: strip stray markdown fences (mirrors the intent extractor)."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:]
        t = t.strip()
    return t


def _coerce_float(value: Any) -> Optional[float]:
    """Step 3 helper: coerce a JSON scalar to a finite float, or ``None``."""
    if isinstance(value, bool):
        return None  # bools are not confidences
    if isinstance(value, (int, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, str):
        try:
            f = float(value.strip())
        except (ValueError, OverflowError):
            return None
        return f if math.isfinite(f) else None
    return None


def _clip(text: str) -> str:
    if len(text) <= _RAW_CLIP:
        return text
    return text[:_RAW_CLIP] + "…[truncated]"


def _repair_params(action_id: str, raw_params: Dict[str, Any]) -> Dict[str, Any]:
    """Steps 5–6: keep only schema keys, then default the optional ones.

    An unknown ``action_id`` has no param schema, so every key is dropped (the
    controller's G3 rejects the action itself). Unknown keys are *never* passed
    through. A required param with no default (``instance_name``) is omitted —
    the guardrail's G5 resolves or rejects it (fallback ``invalid_target``).
    """
    schema = ACTION_SPACE.get(action_id, {}).get("params", {}) or {}
    repaired: Dict[str, Any] = {}
    for name, spec in schema.items():
        if name in raw_params:
            repaired[name] = raw_params[name]
        elif not spec.get("required") and "default" in spec:
            repaired[name] = spec["default"]
    return repaired


_EVIDENCE_FIELD_RE = re.compile(EVIDENCE_FIELD_PATTERN)


def _repair_incident(data: Dict[str, Any]) -> Dict[str, Any]:
    """Light repair of the §4.2 incident record — never rejects the decision.

    Additive within schema 1 (§12): a response with no incident fields is a
    complete response, so every repair here degrades to "absent", never to
    "error". The controller's guardrail (G9) re-runs the same bounds plus the C2
    scan on what this returns; this function only guarantees the *shape* is
    sane enough to hand over.

    An `evidence[]` entry without a usable `field` is dropped rather than
    repaired: a value with no field name is an assertion with no subject, and
    inventing one would attribute a number to a metric the model never named.
    """
    title = data.get("incident_title")
    title = title.strip()[:INCIDENT_TITLE_MAX_CHARS] if isinstance(title, str) else ""

    explanation = data.get("explanation")
    explanation = (
        explanation.strip()[:EXPLANATION_MAX_CHARS] if isinstance(explanation, str) else ""
    )

    evidence: List[Dict[str, Any]] = []
    raw_evidence = data.get("evidence")
    if isinstance(raw_evidence, list):
        for entry in raw_evidence:
            if len(evidence) >= EVIDENCE_MAX_ENTRIES:
                break
            if not isinstance(entry, dict):
                continue
            field = entry.get("field")
            if not isinstance(field, str):
                continue
            field = field.strip()[:EVIDENCE_FIELD_MAX_CHARS]
            if not _EVIDENCE_FIELD_RE.match(field):
                continue
            value = entry.get("value")
            if isinstance(value, bool):
                pass
            elif isinstance(value, (int, float)):
                if not math.isfinite(float(value)):
                    continue
            elif isinstance(value, str):
                value = value.strip()[:EVIDENCE_VALUE_STR_MAX_CHARS]
            else:
                continue
            row: Dict[str, Any] = {"field": field, "value": value}
            seq = entry.get("seq")
            if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0:
                row["seq"] = seq
            ts = entry.get("ts")
            if isinstance(ts, str) and ts.strip():
                row["ts"] = ts.strip()
            evidence.append(row)

    actions: List[Dict[str, Any]] = []
    raw_actions = data.get("actions")
    if isinstance(raw_actions, list):
        for entry in raw_actions:
            if len(actions) >= ACTIONS_MAX_ENTRIES:
                break
            if not isinstance(entry, dict):
                continue
            kind = entry.get("action_kind")
            if not isinstance(kind, str) or not kind.strip():
                continue
            row = {"action_kind": kind.strip()}
            target = entry.get("target")
            if isinstance(target, str) and target.strip():
                row["target"] = target.strip()[:ACTION_TARGET_MAX_CHARS]
            note = entry.get("note")
            if isinstance(note, str) and note.strip():
                row["note"] = note.strip()[:ACTION_NOTE_MAX_CHARS]
            actions.append(row)

    return {"incident_title": title, "explanation": explanation,
            "evidence": evidence, "actions": actions}


def parse(
    content: Any,
    decision_input: Optional[Union[DecisionInput, Dict[str, Any]]] = None,
) -> Union[DecisionResponse, DecisionError]:
    """Parse model completion text into a well-shaped response or a typed error.

    Follows contract §6.2 exactly: fence strip → ``json.loads`` → shape checks
    → param key-pruning + optional-param defaulting → rationale cap. Any step
    that cannot be satisfied yields a :class:`DecisionError` carrying one of the
    §10.1 codes; the caller falls back deterministically.
    """
    del decision_input  # accepted for symmetry/echo; correlation is the caller's

    if not isinstance(content, str):
        return DecisionError(
            REASON_PARSE_ERROR,
            f"model content is {type(content).__name__}, expected a string",
        )

    text = _strip_fences(content)

    # Step 2: JSON.parse.
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return DecisionError(
            REASON_PARSE_ERROR, f"model output is not valid JSON: {exc}", raw=_clip(content)
        )

    if not isinstance(data, dict):
        return DecisionError(
            REASON_SCHEMA_VIOLATION,
            f"top-level JSON is {type(data).__name__}, expected an object",
            raw=_clip(content),
        )

    # Step 4a: schema_version (required, == 1 per §4.1).
    schema_version = data.get("schema_version", SCHEMA_VERSION)
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != SCHEMA_VERSION:
        return DecisionError(
            REASON_SCHEMA_VIOLATION,
            f"schema_version must be the integer {SCHEMA_VERSION}, got {schema_version!r}",
            raw=_clip(content),
        )

    # Step 4b: action_id must be a non-empty string.
    action_id = data.get("action_id")
    if not isinstance(action_id, str) or not action_id.strip():
        return DecisionError(
            REASON_SCHEMA_VIOLATION,
            f"action_id must be a non-empty string, got {action_id!r}",
            raw=_clip(content),
        )
    action_id = action_id.strip()

    # Step 3: confidence coerced to float; non-numeric is a schema violation.
    confidence = _coerce_float(data.get("confidence"))
    if confidence is None:
        return DecisionError(
            REASON_SCHEMA_VIOLATION,
            f"confidence must be a number, got {data.get('confidence')!r}",
            raw=_clip(content),
        )

    # §4.1: rationale is required, 1 .. 1024 chars.
    rationale = data.get("rationale")
    if not isinstance(rationale, str) or len(rationale) < RATIONALE_MIN_CHARS:
        return DecisionError(
            REASON_SCHEMA_VIOLATION,
            f"rationale must be a non-empty string, got {rationale!r}",
            raw=_clip(content),
        )
    # Step 7: cap the rationale to its maximum length.
    rationale = rationale[:RATIONALE_MAX_CHARS]

    # Step 4c: params must be an object (default {}).
    raw_params = data.get("params")
    if raw_params is None:
        raw_params = {}
    if not isinstance(raw_params, dict):
        return DecisionError(
            REASON_SCHEMA_VIOLATION,
            f"params must be an object, got {type(raw_params).__name__}",
            raw=_clip(content),
        )
    params = _repair_params(action_id, raw_params)

    # request_id is optional; it is echoed for tracing and never used for
    # correlation (§4.1).
    request_id = data.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        request_id = None

    # Incident record (§4.2) — additive; absent is valid.
    incident = _repair_incident(data)

    return DecisionResponse(
        action_id=action_id,
        params=params,
        confidence=confidence,
        rationale=rationale,
        schema_version=SCHEMA_VERSION,
        request_id=request_id,
        incident_title=incident["incident_title"],
        explanation=incident["explanation"],
        evidence=incident["evidence"],
        actions=incident["actions"],
    )


# ---------------------------------------------------------------------------
# decide() — render + call + parse, total over transport failures
# ---------------------------------------------------------------------------

def _request_id_of(decision_input: Any) -> Optional[str]:
    if isinstance(decision_input, DecisionInput):
        return decision_input.request_id
    if isinstance(decision_input, dict):
        rid = decision_input.get("request_id")
        return rid if isinstance(rid, str) and rid else None
    return None


def decide(
    decision_input: Union[DecisionInput, Dict[str, Any]],
    client: Optional[ModelClient] = None,
    config: Optional[DecisionModelConfig] = None,
) -> Union[DecisionResponse, DecisionError]:
    """Ask the model for a decision and return a response or a typed error.

    Never raises for a transport/latency/parse problem: timeouts and endpoint
    failures become ``DecisionError`` codes (``inference_timeout`` /
    ``inference_unavailable``) so the caller can fall back promptly without
    blocking remediation on model latency (§6/§8).
    """
    cfg = config or DecisionModelConfig.from_env()
    client = client or default_client(cfg)
    request_id = _request_id_of(decision_input)

    try:
        messages = render(decision_input)
    except (TypeError, ValueError, AttributeError) as exc:
        return DecisionError(
            REASON_SCHEMA_VIOLATION, f"invalid decision input: {exc}"
        )

    try:
        content = client(messages, request_id=request_id)
    except TimeoutError as exc:  # includes socket.timeout (py3.10+)
        return DecisionError(REASON_INFERENCE_TIMEOUT, str(exc) or "model call timed out")
    except urllib.error.HTTPError as exc:
        return DecisionError(
            REASON_INFERENCE_UNAVAILABLE, f"http {exc.code}: {exc.reason}"
        )
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return DecisionError(
                REASON_INFERENCE_TIMEOUT, str(exc.reason) or "model call timed out"
            )
        return DecisionError(REASON_INFERENCE_UNAVAILABLE, str(exc.reason) or str(exc))
    except OSError as exc:
        return DecisionError(REASON_INFERENCE_UNAVAILABLE, str(exc))
    except Exception as exc:  # defensive: a transport surprise must not escape
        return DecisionError(
            REASON_INFERENCE_UNAVAILABLE, f"{type(exc).__name__}: {exc}"
        )

    return parse(content, decision_input)
