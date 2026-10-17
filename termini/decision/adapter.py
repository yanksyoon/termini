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


class Completion(str):
    """The assistant's text PLUS the envelope facts that explain it.

    A ``str`` subclass on purpose: the injectable ``ModelClient`` contract stays
    "returns the content string" — an injected client that returns a plain
    ``str`` still works, and every consumer that treats the completion as text
    (``json.loads``, ``len``, slicing) is unaffected. The default transport
    attaches ``finish_reason`` and ``usage``, which is the difference between
    "the model answered badly" and "the answer was cut off":

    * ``finish_reason == "length"`` means the backend stopped at ``max_tokens``
      and the JSON is incomplete by construction (the live 2026-10-17 failure:
      a 512-token budget for a reply the v2 schema makes ~700-1000 tokens);
    * ``usage.completion_tokens`` is recorded on the decision trace so the next
      budget regression is visible without a packet capture.
    """

    finish_reason: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None

    def __new__(cls, content: str, *, finish_reason: Optional[str] = None,
                usage: Optional[Dict[str, Any]] = None) -> "Completion":
        obj = super().__new__(cls, content)
        obj.finish_reason = finish_reason
        obj.usage = usage if isinstance(usage, dict) else None
        return obj

    @property
    def cut_off(self) -> bool:
        """True when the backend says it stopped at the token limit."""
        return self.finish_reason == "length"


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
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DecisionTransportError(f"unexpected model response shape: {exc}") from exc
        if not isinstance(content, str):
            raise DecisionTransportError(
                f"model content is {type(content).__name__}, expected a string"
            )
        # The envelope facts ride along on a str subclass: `finish_reason` is the
        # only reliable way to tell a truncated reply from a malformed one, and
        # `usage` is what the decision trace records.
        return Completion(
            content,
            finish_reason=choice.get("finish_reason"),
            usage=payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
        )

    return _call


# ---------------------------------------------------------------------------
# Parse + light repair (contract §6.2, steps 1–7)
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    """Step 1: strip stray markdown fences (mirrors the intent extractor).

    Removes a leading opening fence line (``` or ```json …) and a trailing
    closing fence (```), whether or not prose wraps the JSON between them. The
    previous version only stripped a fence whose backticks were the very first
    and last characters of the reply, so a Gemini-style reply that opened with
    "Here is the decision:" or closed with "I hope that helps" was never
    un-fenced and fell through to a parse_error.
    """
    t = text.strip()
    if t.startswith("```"):
        newline = t.find("\n")
        t = t[newline + 1:] if newline != -1 else ""
        t = t.strip()
    if t.endswith("```"):
        t = t[:-3].strip()
    return t


def _extract_json_value(text: str) -> Any:
    """Return the first JSON value in ``text``, or raise ``ValueError``.

    The model may return a clean JSON object, or it may wrap it in a markdown
    fence and/or surrounding prose ("Here is the decision: ```json {…} ``` I
    hope that helps"). Extracting the JSON *value* here does not loosen the
    guardrail: the shape checks in :func:`parse` and the controller's G1/G2
    still reject a value that is not a well-formed decision. It only stops a
    fence or a polite sentence from turning a valid decision into a parse_error
    fallback.

    Only the FIRST structural opener is tried. A later one is almost always
    *inside* a truncated reply — the `{"instance_name": …}` params object of a
    decision that was cut off mid-record — and decoding that fragment produced
    the live 2026-10-17 misdiagnosis "action_id must be a non-empty string, got
    None" for a reply that did carry a valid `action_id` before the cut. A
    fragment is never a decision, so it must not be returned as one.
    """
    s = _strip_fences(text)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # The whole string is not JSON. Scan for the FIRST JSON object (then array)
    # and decode exactly the value that starts there: `raw_decode` skips the
    # leading prose and returns the value plus its end index, ignoring any
    # trailing prose or fence. Objects are preferred over arrays because the
    # contract requires a top-level object.
    decoder = json.JSONDecoder()
    for opener in ("{", "["):
        index = s.find(opener)
        if index < 0:
            continue
        try:
            value, _ = decoder.raw_decode(s, index)
        except json.JSONDecodeError:
            continue
        return value
    raise ValueError("no complete JSON object or array found")


def json_truncated(text: Any) -> bool:
    """True when ``text`` ends inside an unterminated JSON string/object/array.

    This is the *structural* signal of a reply the backend cut off at
    ``max_tokens``: the JSON opens and stops mid-token. It exists so the
    diagnosis does not depend on the transport reporting ``finish_reason`` (an
    injected client returns a bare string), and it is only consulted AFTER the
    text has already failed to parse, so a false positive cannot turn a valid
    decision into an error.
    """
    if not isinstance(text, str) or not text:
        return False
    depth = 0
    in_string = False
    escaped = False
    started = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            started = True
        elif ch in "{[":
            depth += 1
            started = True
        elif ch in "}]":
            depth -= 1
            if depth < 0:
                return False  # unbalanced, but not by truncation
    return started and (in_string or depth > 0)


def completion_cut_off(content: Any) -> bool:
    """True when ``content`` is a truncated completion (either signal)."""
    if getattr(content, "finish_reason", None) == "length":
        return True
    return json_truncated(str(content))


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


def _action_space_params(action_id: str) -> Dict[str, Any]:
    return (ACTION_SPACE.get(action_id, {}).get("params") or {})


def _action_from_actions(data: Dict[str, Any]) -> Dict[str, Any]:
    """The action the reply actually named inside its §4.2 ``actions[]`` entry.

    Some replies carry the whole incident record but omit the top-level
    ``action_id``, naming the chosen action only in ``actions[0]`` (the live
    2026-10-17 shape). Reading it there is a *shape repair*, not a guess:

    * the value must be one of the ids in :data:`ACTION_SPACE` — anything else
      yields nothing, so an invented action is still a schema violation;
    * the controller's guardrail still authorizes it (G3 whitelist, G5 target
      resolution, G6 confidence), so nothing is bypassed — the action merely
      arrives in a different field than the prompt asked for.

    Returns ``{}`` when the reply names no usable action.
    """
    actions = data.get("actions")
    if not isinstance(actions, list):
        return {}
    for entry in actions:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("action_kind")
        if isinstance(kind, str) and kind.strip() in ACTION_SPACE:
            target = entry.get("target")
            return {"action_id": kind.strip(),
                    "target": target.strip() if isinstance(target, str) else ""}
    return {}


def parse(
    content: Any,
    decision_input: Optional[Union[DecisionInput, Dict[str, Any]]] = None,
) -> Union[DecisionResponse, DecisionError]:
    """Parse model completion text into a well-shaped response or a typed error.

    Follows contract §6.2 exactly: fence strip → ``json.loads`` → shape checks
    → param key-pruning + optional-param defaulting → rationale cap. Any step
    that cannot be satisfied yields a :class:`DecisionError` carrying one of the
    §10.1 codes; the caller falls back deterministically.

    A reply that was **truncated** (the backend stopped at ``max_tokens``) is
    reported as a truncation, not as a missing field: saying "action_id must be
    a non-empty string" about a 1458-char reply that carried
    ``"action_id":"instance.restart"`` in its first 90 chars sent the 2026-10-17
    investigation to the wrong place entirely.
    """
    del decision_input  # accepted for symmetry/echo; correlation is the caller's

    if not isinstance(content, str):
        return DecisionError(
            REASON_PARSE_ERROR,
            f"model content is {type(content).__name__}, expected a string",
        )

    # Step 2: JSON.parse — but tolerate the shapes a model actually emits: a
    # fenced ```json block, leading/trailing prose, or a JSON object spanning
    # newlines. Extract the first JSON value; the shape checks below (and the
    # controller's guardrail G1/G2) still reject anything that is not a
    # well-formed decision.
    try:
        data = _extract_json_value(content)
    except ValueError as exc:
        if completion_cut_off(content):
            return DecisionError(
                REASON_PARSE_ERROR,
                f"reply truncated mid-JSON after {len(content)} chars "
                f"(finish_reason={getattr(content, 'finish_reason', None)!r}): "
                f"{exc}",
                raw=_clip(str(content)),
            )
        return DecisionError(
            REASON_PARSE_ERROR, f"model output is not valid JSON: {exc}", raw=_clip(str(content))
        )

    if not isinstance(data, dict):
        return DecisionError(
            REASON_SCHEMA_VIOLATION,
            f"top-level JSON is {type(data).__name__}, expected an object",
            raw=_clip(str(content)),
        )

    # Step 4a: schema_version (required, == 1 per §4.1).
    schema_version = data.get("schema_version", SCHEMA_VERSION)
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != SCHEMA_VERSION:
        return DecisionError(
            REASON_SCHEMA_VIOLATION,
            f"schema_version must be the integer {SCHEMA_VERSION}, got {schema_version!r}",
            raw=_clip(str(content)),
        )

    # Step 4b: action_id must be a non-empty string — or be recoverable from the
    # reply's own `actions[]` record (a shape repair, recorded on the response).
    repairs: List[str] = []
    action_id = data.get("action_id")
    if not isinstance(action_id, str) or not action_id.strip():
        recovered = _action_from_actions(data)
        if recovered:
            action_id = recovered["action_id"]
            repairs.append("action_id taken from actions[0].action_kind")
        else:
            return DecisionError(
                REASON_SCHEMA_VIOLATION,
                f"action_id must be a non-empty string, got {action_id!r}",
                raw=_clip(str(content)),
            )
    action_id = action_id.strip()

    # Step 3: confidence coerced to float; non-numeric is a schema violation.
    confidence = _coerce_float(data.get("confidence"))
    if confidence is None:
        return DecisionError(
            REASON_SCHEMA_VIOLATION,
            f"confidence must be a number, got {data.get('confidence')!r}",
            raw=_clip(str(content)),
        )

    # §4.1: rationale is required, 1 .. 1024 chars.
    rationale = data.get("rationale")
    if not isinstance(rationale, str) or len(rationale) < RATIONALE_MIN_CHARS:
        return DecisionError(
            REASON_SCHEMA_VIOLATION,
            f"rationale must be a non-empty string, got {rationale!r}",
            raw=_clip(str(content)),
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
            raw=_clip(str(content)),
        )
    params = _repair_params(action_id, raw_params)

    # The required instance target may also live only in `actions[0].target`.
    # Kept as a shape repair for the same reason as the action id: the guardrail
    # resolves the target against the live inventory (G5) and rejects it when it
    # is not a real instance, so a wrong name cannot leak through here.
    if "instance_name" in _action_space_params(action_id) and "instance_name" not in params:
        target = _action_from_actions(data).get("target")
        if target:
            params["instance_name"] = target
            repairs.append("params.instance_name taken from actions[0].target")

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
        raw=str(content),
        usage=getattr(content, "usage", None),
        finish_reason=getattr(content, "finish_reason", None),
        repairs=repairs,
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


# ---------------------------------------------------------------------------
# Repair retry (one shot, bounded) — the answer that arrived malformed
# ---------------------------------------------------------------------------

# Why this exists: a decision that fails the shape check is answered by the
# deterministic playbook, and on camera that reads as "the AI did not decide".
# A single, bounded second question to the same model turns the two mechanical
# failures — a reply cut off at `max_tokens`, and an incident-only reply that
# names its action in `actions[]` but not at the top level — back into a model
# decision, without ever inventing one: if the retry is also unusable, the
# caller still falls back and the fallback stays labelled as one.
REPAIR_TRUNCATED = "truncated"
REPAIR_NO_ACTION = "no_action"
REPAIR_NO_RATIONALE = "no_rationale"

_REPAIR_INSTRUCTIONS: Dict[str, str] = {
    REPAIR_TRUNCATED: (
        "Your previous reply was cut off before the JSON object closed (it hit "
        "the max_tokens limit). Return ONE complete JSON object now and keep it "
        "short: at most 2 evidence entries, explanation <= 240 chars, rationale "
        "<= 240 chars. Keep the same action_id, params and confidence."
    ),
    REPAIR_NO_ACTION: (
        "Your previous reply was an incident record with no top-level action_id. "
        "Return ONE complete JSON object that INCLUDES action_id (exactly one of "
        "the allowed action ids), params, confidence and rationale, plus the "
        "incident fields."
    ),
    REPAIR_NO_RATIONALE: (
        "Your previous reply omitted the required rationale. Return ONE complete "
        "JSON object with action_id, params, confidence and a one-sentence "
        "rationale."
    ),
}


def _repair_kind(result: Union[DecisionResponse, DecisionError], content: Any) -> Optional[str]:
    """Which repair retry (if any) a failed parse justifies.

    Only the *mechanical* shapes are retried. A reply that is off-schema in a
    way a retry cannot fix (an action outside the space, a non-numeric
    confidence) is returned as its own error — the caller falls back and the
    fallback stays visible.
    """
    if not isinstance(result, DecisionError):
        return None
    if result.reason not in (REASON_PARSE_ERROR, REASON_SCHEMA_VIOLATION):
        return None
    if completion_cut_off(content):
        return REPAIR_TRUNCATED
    detail = (result.detail or "").lower()
    if "action_id" in detail and ("non-empty" in detail or "missing" in detail):
        return REPAIR_NO_ACTION
    if "rationale" in detail:
        return REPAIR_NO_RATIONALE
    return None


def _call_client(client: ModelClient, messages: List[Dict[str, str]],
                 request_id: Optional[str]) -> tuple[Optional[str], Optional[DecisionError]]:
    """Call the transport and map every failure onto a §10.1 code.

    Never raises: timeouts and endpoint failures become ``DecisionError`` values
    so the caller can fall back promptly without blocking remediation on model
    latency (§6/§8).
    """
    try:
        content = client(messages, request_id=request_id)
    except TimeoutError as exc:  # includes socket.timeout (py3.10+)
        return None, DecisionError(REASON_INFERENCE_TIMEOUT, str(exc) or "model call timed out")
    except urllib.error.HTTPError as exc:
        return None, DecisionError(
            REASON_INFERENCE_UNAVAILABLE, f"http {exc.code}: {exc.reason}"
        )
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return None, DecisionError(
                REASON_INFERENCE_TIMEOUT, str(exc.reason) or "model call timed out"
            )
        return None, DecisionError(REASON_INFERENCE_UNAVAILABLE, str(exc.reason) or str(exc))
    except OSError as exc:
        return None, DecisionError(REASON_INFERENCE_UNAVAILABLE, str(exc))
    except Exception as exc:  # defensive: a transport surprise must not escape
        return None, DecisionError(
            REASON_INFERENCE_UNAVAILABLE, f"{type(exc).__name__}: {exc}"
        )
    return content, None


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

    One bounded repair retry is made when the reply arrived *mechanically*
    malformed (cut off at ``max_tokens``, or with the action only in
    ``actions[]``). The retry appends the first answer and a corrective
    instruction to the same conversation; a retry that also fails is returned as
    its own ``DecisionError``, so a failed decision is never dressed up as a
    model decision.
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

    content, err = _call_client(client, messages, request_id)
    if err is not None:
        return err
    result = parse(content, decision_input)

    kind = _repair_kind(result, content)
    if kind is None:
        return result

    repair_messages = list(messages) + [
        {"role": "assistant", "content": str(content)},
        {"role": "user", "content": _REPAIR_INSTRUCTIONS[kind]},
    ]
    content2, err2 = _call_client(client, repair_messages, request_id)
    if err2 is not None:
        return err2
    result2 = parse(content2, decision_input)
    if isinstance(result2, DecisionResponse):
        result2.repairs.append(f"answered on the repair retry ({kind})")
        return result2

    # Both attempts failed. Report the second (it is the reply the corrective
    # instruction produced) and name the first in the detail, so the record says
    # what the model actually did twice.
    return DecisionError(
        result2.reason,
        f"{result2.detail} [repair retry after {kind}; first attempt: {result.detail}; "
        f"attempt lengths {len(content)} / {len(content2)} chars]",
        raw=result2.raw,
    )

