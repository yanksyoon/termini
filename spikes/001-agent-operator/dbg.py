#!/usr/bin/env python3
"""Debug: print the generated GBNF and the RAW constrained emission."""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.abspath(os.path.join(HERE, "..", "..", "controller")), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)
import agent, gbnf
import probe  # reuse its synthetic event

targets = agent._candidates(probe.OOM_EVENT)
g = gbnf.gbnf_for_tool_call(targets=targets)
print("=== GENERATED GBNF ===")
print(g)
print("=== note the literal-quote rule above ===")
g0 = g.splitlines()[0]
print("root line, repr of first 120 chars:")
print(repr(g0[:120]))
print()

user = ("State snapshot (C0 telemetry only):\n"
        + json.dumps(agent._digest(probe.OOM_EVENT), separators=(",", ":"))
        + "\n\nDecide the single best action.")

raw = agent._call_llm(g, user)
print("=== RAW CONSTRAINED EMISSION ===")
print(repr(raw))
print()
print("=== as text ===")
print(raw)
