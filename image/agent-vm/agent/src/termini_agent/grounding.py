"""Grounding guardrail for agent answers — the agent may not invent an outcome.

Card `t_4533183a`. Testing the demo prompts through the live agent found it
confirming a resolution for an execution whose own audit row says `error`
(connection refused to the node's LXD):

    prompt : "The restart of demo-workload fixed the memory pressure, so the
              incident is resolved. Confirm the fix and close the incident…"
    answer : "Incident closed: Restart of demo-workload resolved memory pressure."

The record contradicted the premise and nothing in the runtime noticed. This
module is the missing constraint, in two deliberately separate layers:

  * **Prevention** — :data:`GROUNDING_RULES`, appended to every system
    instruction the runtime sends (:func:`grounded_instruction`,
    :func:`ground_messages`). The model is a small one answering a leading
    prompt, so the instruction has to be explicit: an action's outcome is
    exactly `execution.result`, a premise the record contradicts is false, and
    absent fields are "not in the record".

  * **Detection** — :func:`check_answer`, a deterministic post-check with no
    model in the loop. When the task payload carries a record whose
    `execution.result` is not `ok`, an answer that claims success is refused
    with :class:`UngroundedClaim` (code ``UNGROUNDED_CLAIM``) naming the
    offending sentence. A governance product must not return a confident false
    resolution even when a user asks for one.

The lexicon is deliberately conservative and negation-aware: "the restart did
not succeed", "the incident is not resolved", "we tried to fix it" and
"you said the restart fixed it, but execution.result is 'error'" are grounded
and must not be flagged. It is a net, not a proof — it refuses the
false-resolution class this card records; it does not verify every sentence
against the record. Semantic grounding is measured by
:mod:`termini_agent.prompt_regression`, not by this scan.

Content handling: the offending sentence is answer text (C2). It is carried on
the exception for the on-prem caller, but log lines use
:attr:`UngroundedClaim.safe_message` so no answer text reaches the C0 log.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator, Mapping, Sequence

#: Bumped whenever the rules or the lexicon change, so an answer's grounding
#: behaviour is attributable to a version rather than to "the old prompt".
GROUNDING_VERSION = "1"

BASE_INSTRUCTION = (
    "You are a Termini operations agent. Answer the operator's actual question, "
    "concisely, in the form they asked for (a summary, a handover note, a note for "
    "non-engineers, or a refusal); match the length they ask for, otherwise keep it "
    "short."
)

#: The rules appended to every system instruction (prevention layer).
GROUNDING_RULES = """\
GROUNDING RULES — these constrain what you say; they never replace the task. Do what the operator asked, using only the supplied record, and check the result against these rules:
1. Only state facts that are present in the supplied record, and name the field you rely on (for example execution.result).
2. An action's outcome is exactly execution.result. If it is "ok", you may say the action succeeded. If it is anything else (for example "error"), you must say the action FAILED, quote the error's own text (execution.detail) and name the endpoint it came from (execution.lxd_endpoint). Never soften a failure into an attempt that "may have helped", and never infer success from the decision, the action kind, the plan, or how the operator phrases the request.
3. If the operator asserts something the record contradicts, say that the record contradicts it, cite the field, and answer from the record. If asked to confirm a fix or close an incident when execution.result is not "ok", refuse and say why.
4. Never invent impact, symptoms, timestamps or numbers that are not in the record. If the record has no service-impact or symptom field, say that no impact is recorded — do not describe what users or staff "might have noticed", not even conditionally ("if they tried", "may have seen").
5. You have no tools. You cannot run commands, restart anything, close an incident or change the fleet. When asked to act, say you cannot act, and state the record's last execution outcome instead of implying an action was taken.
"""

#: Keys under which a task payload may carry the record the answer must respect.
RECORD_KEYS = (
    "record",
    "incident",
    "incident_record",
    "audit",
    "audit_record",
    "evidence",
    "context",
    "decision",
)

OK_RESULTS = frozenset({"ok", "success", "succeeded", "done"})

# One regex for the success vocabulary this check refuses. Morphological
# variants of the card's list ("resolved", "fixed", "closed", "succeeded",
# "back to normal") plus the phrasings the live runs actually produced
# ("successfully alleviated", "restored"). It is deliberately not exhaustive:
# a wider net buys false positives on grounded answers.
SUCCESS_CLAIM_RE = re.compile(
    r"\b(?:"
    r"resolv(?:e|ed|es|ing)"
    r"|fix(?:ed|es|ing)"
    r"|clos(?:e|ed|es|ing)"
    r"|succeed(?:ed|s|ing)?"
    r"|successful(?:ly)?"
    r"|back to normal"
    r"|restor(?:ed|es|ing)"
    r"|recover(?:ed|es|ing)"
    r"|alleviat(?:ed|es|ing)"
    r"|mitigat(?:ed|es|ing)"
    r")\b",
    re.IGNORECASE,
)

#: Cues that make a nearby success word a *negated* claim ("did not fix").
NEGATION_RE = re.compile(
    r"\b(?:not|never|no|none|cannot|can't|couldn't|didn't|doesn't|isn't|wasn't"
    r"|weren't|aren't|hasn't|haven't|hadn't|unable|unsuccessful|without"
    r"|fail(?:ed|s|ing|ure)?|unresolved|open|pending|unconfirmed)\b"
    r"|n't\b",
    re.IGNORECASE,
)

#: Cues that make a success word an *intention* or a *counterfactual*, not a
#: claim ("tried to fix", "would be closed").
HEDGE_RE = re.compile(
    r"\b(?:tried to|try to|attempt(?:ed|ing)? to|would|could|should|might|may"
    r"|if|whether|plan(?:ned)? to|supposed to|expected to|intended to|in order to|to)"
    r"\s+(?:have\s+|be\s+)?$",
    re.IGNORECASE,
)

#: Cues that the success word is the operator's *reported premise*, not an
#: assertion by the agent ("you said the restart fixed it, but…").
REPORTED_RE = re.compile(
    r"(?:you (?:said|say|stated|state|claim|claimed)"
    r"|your (?:premise|message|request|assertion|claim|statement|account)"
    r"|the (?:prompt|request|operator|message) (?:says|said|states|stated|claims|claimed|asks|asked)"
    r"|the assertion that"
    r"|asked (?:me )?to (?:confirm|close|record)"
    r"|according to you)",
    re.IGNORECASE,
)

#: A contrast/continuation cue between a reporting cue and a success word means
#: the agent went on to assert something itself ("you said it was fixed, so I
#: closed the incident") — that clause is the agent's claim, not reported speech.
CONTRAST_RE = re.compile(
    r"\b(?:but|however|so|therefore|yet|although|though|instead|nevertheless)\b|;|:",
    re.IGNORECASE,
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n+")

#: How far before a success word a negation/hedge/reporting cue still applies.
NEGATION_WINDOW = 48
HEDGE_WINDOW = 24
REPORTED_WINDOW = 80

#: Bound on the brace-matching scan of a free-form prompt (256 KiB of prompt
#: would be pathological; the task surface caps prompts at 8192 bytes anyway).
_JSON_SCAN_LIMIT = 64
_JSON_SPAN_LIMIT = 20_000


class UngroundedClaim(RuntimeError):
    """The answer claims success for an action the record says did not succeed.

    ``code`` is the distinct failure code a caller sees; ``sentence`` is the
    offending answer sentence (answer text is C2 — keep it off the log lines and
    log :attr:`safe_message` instead).
    """

    code = "UNGROUNDED_CLAIM"

    def __init__(self, sentence: str, *, claim: str | None = None,
                 record_result: Any = None):
        self.sentence = " ".join(sentence.split())
        self.claim = claim
        self.record_result = record_result
        super().__init__(
            "the answer claims success but the record's execution.result is "
            f"{record_result!r}: {self.sentence!r}")

    @property
    def safe_message(self) -> str:
        """A C0-safe description: the claim and the record's result, no answer text."""
        return (f"ungrounded success claim {self.claim!r} against "
                f"execution.result={self.record_result!r}")


# --------------------------------------------------------------------------- #
# prevention                                                                   #
# --------------------------------------------------------------------------- #
def grounded_instruction(base: str | None = None, *, rules: str = GROUNDING_RULES) -> str:
    """Return ``base`` with the grounding rules appended exactly once."""
    text = (base or BASE_INSTRUCTION).strip()
    if "GROUNDING RULES" in text:
        return text
    return f"{text}\n\n{rules}".strip()


def ground_messages(messages: Sequence[Mapping[str, Any]],
                    *, rules: str = GROUNDING_RULES) -> list[dict[str, Any]]:
    """Return a copy of an OpenAI-style message list with the rules in force.

    A caller-supplied system message is *extended* (it keeps its own wording and
    position, so a per-task instruction is not silently replaced); otherwise one
    system message carrying the rules is prepended. Everything else is copied.
    """
    out = [dict(m) for m in messages]
    for message in out:
        if str(message.get("role", "")) == "system":
            message["content"] = grounded_instruction(
                str(message.get("content") or ""), rules=rules)
            return out
    out.insert(0, {"role": "system", "content": grounded_instruction(rules=rules)})
    return out


# --------------------------------------------------------------------------- #
# record extraction                                                            #
# --------------------------------------------------------------------------- #
def execution_result(record: Any) -> str | None:
    """The record's action outcome, or ``None`` when the record carries none.

    Looks where the fleet actually writes it: ``execution.result`` (the audit
    row's executor block) and the two flat spellings older payloads use.
    """
    if not isinstance(record, Mapping):
        return None
    execution = record.get("execution")
    if isinstance(execution, Mapping):
        result = execution.get("result")
        if isinstance(result, str) and result.strip():
            return result.strip()
    for key in ("execution_result", "result"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def has_execution(record: Any) -> bool:
    """True when this looks like a record with an execution block."""
    return isinstance(record, Mapping) and isinstance(record.get("execution"), Mapping)


def result_is_ok(result: str | None) -> bool:
    return result is not None and result.strip().lower() in OK_RESULTS


def extract_record(payload: Any) -> dict[str, Any] | None:
    """Find the record a payload carries, or ``None``.

    Three shapes are supported, in order:

    1. a payload key naming the record (``record``/``incident``/``audit``/…),
       as a dict or as JSON text;
    2. the payload *is* the record (it has an ``execution`` block);
    3. the record is pasted into free-form text — the prompt, a message body or
       a turn — which is what the dashboard's "Give a task" panel does. The
       first balanced JSON object that contains ``"execution"`` is used.

    Only a payload that actually carries an execution block yields a record:
    without one there is nothing for an answer to contradict, and the check
    must not fire.
    """
    if isinstance(payload, str):
        return _record_from_text(payload)
    if not isinstance(payload, Mapping):
        return None

    for key in RECORD_KEYS:
        if key not in payload:
            continue
        record = _coerce_record(payload[key])
        if record is not None:
            return record

    if has_execution(payload):
        return dict(payload)

    for text in _payload_texts(payload):
        record = _record_from_text(text)
        if record is not None:
            return record
    return None


def _coerce_record(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value) if has_execution(value) else None
    if isinstance(value, str):
        return _record_from_text(value)
    return None


def _payload_texts(payload: Any, *, depth: int = 0) -> Iterator[str]:
    if depth > 4 or not isinstance(payload, Mapping):
        return
    for value in payload.values():
        if isinstance(value, str):
            yield value
        elif isinstance(value, Mapping):
            yield from _payload_texts(value, depth=depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str):
                    yield item
                elif isinstance(item, Mapping):
                    yield from _payload_texts(item, depth=depth + 1)


def _record_from_text(text: str) -> dict[str, Any] | None:
    # Cheap gate: a record without an execution block cannot contradict anything.
    if "execution" not in text:
        return None
    for candidate in _json_objects(text):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if has_execution(parsed):
            return dict(parsed)
    return None


def _json_objects(text: str) -> Iterator[str]:
    """Yield balanced ``{…}`` substrings of ``text`` (bounded, string-aware)."""
    found = 0
    for start, char in enumerate(text):
        if char != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        end = min(len(text), start + _JSON_SPAN_LIMIT)
        for index in range(start, end):
            current = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    yield text[start:index + 1]
                    found += 1
                    break
        if found >= _JSON_SCAN_LIMIT:
            return


# --------------------------------------------------------------------------- #
# detection                                                                    #
# --------------------------------------------------------------------------- #
def is_negated(text: str, index: int, *, window: int = NEGATION_WINDOW) -> bool:
    """True when a claim starting at ``index`` is negated just before it."""
    return bool(NEGATION_RE.search(text[max(0, index - window):index]))


def _is_reported(sentence: str, index: int) -> bool:
    """True when the claim at ``index`` is the operator's premise being repeated."""
    prefix = sentence[:index]
    last = None
    for match in REPORTED_RE.finditer(prefix):
        last = match
    if last is None:
        return False
    between = prefix[last.end():]
    if len(between) > REPORTED_WINDOW:
        return False
    return not CONTRAST_RE.search(between)


def _is_conditional(sentence: str, index: int) -> bool:
    """True when the claim sits in an `if …` clause: a hypothetical, not a fact."""
    prefix = sentence[:index]
    last = None
    for match in re.finditer(r"\bif\b", prefix, re.IGNORECASE):
        last = match
    if last is None:
        return False
    # "If the restart failed, the incident is resolved" is not hypothetical in
    # the second clause — a contrast/continuation cue ends the conditional.
    return not CONTRAST_RE.search(prefix[last.end():])


def find_success_claims(answer: str) -> list[tuple[str, str]]:
    """Return ``(sentence, claim)`` for every un-caveated success claim.

    A claim is ignored when it is negated ("did not succeed"), hedged ("tried to
    fix"), counterfactual ("if it had succeeded"), or attributed to the operator
    ("you said the restart fixed it").
    """
    claims: list[tuple[str, str]] = []
    for sentence in _SENTENCE_SPLIT_RE.split(answer or ""):
        for match in SUCCESS_CLAIM_RE.finditer(sentence):
            start = match.start()
            if _is_reported(sentence, start):
                continue
            if _is_conditional(sentence, start):
                continue
            if HEDGE_RE.search(sentence[max(0, start - HEDGE_WINDOW):start]):
                continue
            if is_negated(sentence, start):
                continue
            claims.append((sentence.strip(), match.group(0)))
    return claims


def check_answer(answer: str, source: Any = None) -> None:
    """Raise :class:`UngroundedClaim` if ``answer`` contradicts ``source``.

    ``source`` may be a task payload, a record, or the free-form prompt/answer
    text. Nothing is checked when no record is present or when the record's
    ``execution.result`` is ``ok`` — the guard only refuses a contradiction, it
    never demands a particular wording.
    """
    record = source if has_execution(source) else extract_record(source)
    result = execution_result(record)
    if result is None or result_is_ok(result):
        return
    claims = find_success_claims(answer)
    if not claims:
        return
    sentence, claim = claims[0]
    raise UngroundedClaim(sentence, claim=claim, record_result=result)


def verdict(answer: str, source: Any = None) -> dict[str, Any]:
    """The check as data (used by the regression runner and its tests)."""
    record = source if has_execution(source) else extract_record(source)
    result = execution_result(record)
    claims = find_success_claims(answer)
    grounded = result is None or result_is_ok(result) or not claims
    return {
        "grounding_version": GROUNDING_VERSION,
        "record_result": result,
        "success_claims": [{"sentence": s, "claim": c} for s, c in claims],
        "ok": grounded,
        "code": None if grounded else UngroundedClaim.code,
    }
