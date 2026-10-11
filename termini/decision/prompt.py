"""Static, versioned prompt for the model-driven operator decision step.

Template ``operator-decision v1`` (``docs/contracts/operator-decision.md`` §6.1).
The template is frozen and ``governance.md`` §7.2-compliant: it is a static
system prompt containing only typed, C0 prompt placeholders, and its single
dynamic placeholder (``{{decision_input}}``) receives the C0 ``DecisionInput``
serialized as compact JSON.

The action enumeration in the system prompt is **generated from**
:data:`termini.decision.types.ACTION_SPACE`, never hard-coded, so the prompt and
the controller guardrail can never disagree about the allowed action space.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Union

from .types import ACTION_SPACE, DecisionInput

TEMPLATE_VERSION = "operator-decision v1"

# The template's single typed placeholder (§6.1).
DECISION_INPUT_PLACEHOLDER = "{{decision_input}}"

# The exact shape the model must emit (§4). Kept textual so it can be embedded
# verbatim in the system prompt.
DECISION_JSON_SCHEMA = """{
  "schema_version": 1,
  "request_id": "<echo the request_id from the input>",
  "action_id": "<exactly one allowed action id>",
  "params": { "<param>": "<value>" },
  "confidence": <number between 0.0 and 1.0>,
  "rationale": "<one sentence explaining the choice, <= 1024 chars>"
}""".strip()


def _render_action_space() -> str:
    """Render the allowed action space from the canonical ACTION_SPACE (§5).

    Generated, not hard-coded, so adding an action to ``ACTION_SPACE`` is the
    only edit needed for the prompt to advertise it.
    """
    lines: List[str] = []
    for action_id, spec in ACTION_SPACE.items():
        params = spec.get("params") or {}
        if not params:
            lines.append(f"- \"{action_id}\" (takes no params, emit \"params\": {{}})")
            continue
        parts: List[str] = []
        for name, p in params.items():
            desc = f'{name}: {p.get("type", "any")}'
            if p.get("required"):
                desc += " (required)"
            elif "default" in p:
                desc += f' (optional, default {json.dumps(p["default"])})'
            if p.get("min") is not None and p.get("max") is not None:
                desc += f', clamped to {p["min"]}..{p["max"]}'
            if p.get("resolve") == "instance":
                desc += (
                    ' — either the literal "heaviest" (the instance with the '
                    "highest ram_used_bytes across the evidence window) or a "
                    "vm_id / instance_name present in target_resource.instances[]"
                )
            parts.append(desc)
        lines.append(f'- "{action_id}": ' + "; ".join(parts))
    return "\n".join(lines)


ACTION_SPACE_TEXT = _render_action_space()

SYSTEM_PROMPT = f"""You are the Termini operator decision model (template {TEMPLATE_VERSION}).

A workload cluster has raised a health event on one node. Choose the single best
remediation action for that node from the allowed action space and answer in
JSON. You are the decision step of an automated self-healing loop: your answer
may be executed by the node's LXD controller after a deterministic guardrail has
validated it, so it must name exactly one action and a concrete, in-scope target.

Allowed action space:
{ACTION_SPACE_TEXT}

Rules:
- Output ONLY one JSON object, no prose, no markdown fences, no trailing text.
- "action_id" MUST be exactly one of the quoted action ids above, and MUST be
  present in the input's "allowed_action_space".
- "params" MUST contain only keys that belong to the chosen action. For every
  instance-scoped action, "instance_name" must be either "heaviest" or an exact
  vm_id / instance_name from "target_resource"."instances" — never invent a name.
- "confidence" MUST be a number in 0.0 .. 1.0 reflecting how well the evidence
  supports the choice.
- "rationale" MUST be one short, factual sentence (<= 1024 chars) naming the
  metric that breached and why this action fixes it. Do not include personal
  data or application payloads.
- Prefer the least destructive action that resolves the fault: restart before
  stop, stop before delete, prune images before touching instances.
- If the evidence does not justify any remediation, choose the least destructive
  action with a low confidence (the downstream guardrail will fall back).

Return exactly this shape:

{DECISION_JSON_SCHEMA}"""

# The user message template: the C0 DecisionInput as compact JSON, embedded in
# the template's single {{decision_input}} placeholder.
USER_TEMPLATE = (
    "Health event decision request. Decide the remediation action for this "
    "input:\n\n{{decision_input}}\n\n"
    "Return the JSON decision object and nothing else."
)

if USER_TEMPLATE.count(DECISION_INPUT_PLACEHOLDER) != 1:
    raise AssertionError(
        "operator-decision template must contain exactly one "
        f"{DECISION_INPUT_PLACEHOLDER} placeholder"
    )


def render_user(decision_input: Union[DecisionInput, Dict[str, Any]]) -> str:
    """Render the user message: the compact-JSON DecisionInput in the template."""
    if isinstance(decision_input, DecisionInput):
        payload: Dict[str, Any] = decision_input.to_dict()
    else:
        payload = dict(decision_input or {})
    return USER_TEMPLATE.replace(
        DECISION_INPUT_PLACEHOLDER, compact_json(payload)
    )


def compact_json(payload: Any) -> str:
    """Serialize a payload as compact JSON (no whitespace), key order preserved."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


# Few-shot examples: one canonical OOM restart (the §3/§4 demo case) and one
# disk-degrade stop. Assistant turns are the exact JSON the model must produce.
FEW_SHOT: List[Dict[str, str]] = [
    {
        "user": (
            "An \"oom\" event fired on node 01M4349RPTT29RNTC6VBCXQ0RC: host RAM "
            "is at 94.7% and one instance (agent-01H4KZ9Q) holds 29% of RAM. "
            "Choose a remediation."
        ),
        "assistant": compact_json(
            {
                "schema_version": 1,
                "request_id": "01M4349RQ09HMAX6RC9YPYQH4W",
                "action_id": "instance.restart",
                "params": {
                    "instance_name": "agent-01H4KZ9Q",
                    "force": True,
                    "timeout_s": 30,
                },
                "confidence": 0.87,
                "rationale": (
                    "host RAM at 94.7% of total with one instance holding 29%; "
                    "restarting the heaviest instance reclaims its memory."
                ),
            }
        ),
    },
    {
        "user": (
            "A \"disk_degrade\" event fired: /dev/sda write latency is 48ms and "
            "rising with no read-only mount. Choose a remediation."
        ),
        "assistant": compact_json(
            {
                "schema_version": 1,
                "request_id": "01M4349RQ09HMAX6RC9YPYQH4W",
                "action_id": "instance.stop",
                "params": {"instance_name": "heaviest", "force": True},
                "confidence": 0.71,
                "rationale": (
                    "sustained write-latency rise on /dev/sda with the disk still "
                    "writable; stopping the heaviest writer sheds the I/O load."
                ),
            }
        ),
    },
]


def build_messages(decision_input: Union[DecisionInput, Dict[str, Any]]) -> List[Dict[str, str]]:
    """Build the OpenAI-style message list for a single decision call.

    Shape (§6.1): a static system prompt, the few-shot pairs, then the user
    message carrying the C0 ``DecisionInput`` as compact JSON.
    """
    messages: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for example in FEW_SHOT:
        messages.append({"role": "user", "content": example["user"]})
        messages.append({"role": "assistant", "content": example["assistant"]})
    messages.append({"role": "user", "content": render_user(decision_input)})
    return messages
