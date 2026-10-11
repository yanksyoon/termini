"""GBNF grammar generation for the operator's tool call.

The safety property this spike tests is structural, not filtering:

    the model is *incapable* of emitting an action outside the allowed set,
    because the decoder is constrained to a grammar in which no other
    token sequence exists.

That is stronger than parsing JSON and validating it afterwards (which can
fail open) and stronger than a prompt instruction (which can be ignored).

GBNF is llama.cpp's grammar format. We generate it per-call so the candidate
instance list is baked into the grammar itself -- the model cannot name a
workload that does not exist, because no such production exists in the grammar.
"""
from __future__ import annotations

# The executor's real verbs (controller/app/operator/executor.py) plus two
# agent-only terminals that change no state.
AGENT_ACTIONS = (
    "instance.restart",
    "instance.stop",
    "instance.delete",
    "image.prune",
    "escalate_to_human",
    "no_action",
)

NONE = "none"


def _lit(s: str) -> str:
    """A GBNF double-quoted literal (GBNF escapes '"' as '\\"')."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _json_str(s: str) -> str:
    """A GBNF literal matching a JSON *string value* -- quotes included.

    `_lit("x")` matches the bare text `x`; a JSON value needs `"x"`. Getting
    this wrong yields a grammar that is honoured but emits invalid JSON, which
    is exactly the bug this spike hit first.
    """
    return _lit('"' + s + '"')


def _enum(values: list[str]) -> str:
    return " | ".join(_json_str(v) for v in values)


def gbnf_for_tool_call(actions: tuple[str, ...] = AGENT_ACTIONS,
                       targets: list[str] | None = None,
                       max_evidence: int = 4) -> str:
    """Return a GBNF grammar matching exactly one JSON tool call.

    Shape (fixed key order -- GBNF cannot express unordered objects):
        {"action": <enum>, "target": <enum>, "confidence": <0..1>,
         "rationale": <short string>, "evidence": [<string>, ...]}
    """
    targets = list(targets or [])
    # `target` is an enum of the vm_ids actually present in the evidence window,
    # plus `none`. Nothing else is expressible.
    target_values = (targets + [NONE]) if targets else [NONE]

    return "\n".join([
        "root ::= "
        + _lit("{")
        + ' ws ' + _lit('"action"') + ' ws ' + _lit(":") + ' ws action'
        + ' ws ' + _lit(",")
        + ' ws ' + _lit('"target"') + ' ws ' + _lit(":") + ' ws target'
        + ' ws ' + _lit(",")
        + ' ws ' + _lit('"confidence"') + ' ws ' + _lit(":") + ' ws confidence'
        + ' ws ' + _lit(",")
        + ' ws ' + _lit('"rationale"') + ' ws ' + _lit(":") + ' ws string'
        + ' ws ' + _lit(",")
        + ' ws ' + _lit('"evidence"') + ' ws ' + _lit(":") + ' ws evidence'
        + ' ws ' + _lit("}"),
        f"action ::= {_enum(list(actions))}",
        f"target ::= {_enum(target_values)}",
        # JSON number in [0,1]; no exponent, no leading '+', no bare '.'.
        'confidence ::= "0" | "1" | "0." digit{1,6}',
        f'evidence ::= "[" ws (string (ws "," ws string){{0,{max_evidence}}})? ws "]"',
        r'string ::= "\"" char* "\""',
        # Deliberately escape-free: no backslashes, no quotes inside strings.
        # Keeps the grammar (and therefore the model's job) small.
        r'char ::= [a-zA-Z0-9 .,;:_%()/+\-=<>!?]',
        "digit ::= [0-9]",
        r"ws ::= [ \t\n]*",
        "",
    ])


def gbnf_for_freeform() -> str:
    """A deliberately permissive grammar used to test the constraint itself.

    Same prefix as the tool call, but `action` is any short string -- i.e. the
    unsafe alternative. Used by the negative test to show what the model WOULD
    emit without the enum.
    """
    return "\n".join([
        "root ::= "
        + _lit("{")
        + ' ws ' + _lit('"action"') + ' ws ' + _lit(":") + ' ws string'
        + ' ws ' + _lit(",")
        + ' ws ' + _lit('"target"') + ' ws ' + _lit(":") + ' ws string'
        + ' ws ' + _lit(",")
        + ' ws ' + _lit('"confidence"') + ' ws ' + _lit(":") + ' ws confidence'
        + ' ws ' + _lit(",")
        + ' ws ' + _lit('"rationale"') + ' ws ' + _lit(":") + ' ws string'
        + ' ws ' + _lit(",")
        + ' ws ' + _lit('"evidence"') + ' ws ' + _lit(":") + ' ws evidence'
        + ' ws ' + _lit("}"),
        'confidence ::= "0" | "1" | "0." digit{1,6}',
        'evidence ::= "[" ws (string (ws "," ws string){0,4})? ws "]"',
        r'string ::= "\"" char* "\""',
        r'char ::= [a-zA-Z0-9 .,;:_%()/+\-=<>!?]',
        "digit ::= [0-9]",
        r"ws ::= [ \t\n]*",
        "",
    ])


if __name__ == "__main__":
    import sys
    targets = sys.argv[1:] or ["01ABCDEF", "01GHIJKL"]
    print(gbnf_for_tool_call(targets=targets))
