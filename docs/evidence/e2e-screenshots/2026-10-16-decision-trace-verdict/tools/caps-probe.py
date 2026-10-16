#!/usr/bin/env python3
"""Independent probe: controller-side trace caps (detail 512, reply 4096+flag,
max 24 steps). Uses a reply string with spaces so it is NOT a base64 blob."""
import os, pathlib, sys
REPO = pathlib.Path(os.environ["REPO"]).resolve()
sys.path.insert(0, str(REPO / "controller")); sys.path.insert(0, str(REPO))
from app.operator import decision

t = decision.DecisionTrace(decided_by="model")
rep = t.add_reply("model reply received", "word " * 1000)  # 5000 chars, no b64 run
print("reply len:", len(rep["detail"]), "truncated:", rep.get("truncated"))
assert len(rep["detail"]) == 4096 and rep["truncated"] is True

t2 = decision.DecisionTrace(decided_by="model")
s = t2.add("gates", "g", "y " * 300)  # 600 chars, no b64 run
print("detail len:", len(s["detail"]))
assert len(s["detail"]) == 512

t3 = decision.DecisionTrace(decided_by="model")
for i in range(40):
    t3.add("gates", f"g{i}", f"d{i}")
print("step count:", len(t3.steps))
assert len(t3.steps) == 24
print("OK: controller caps enforced (detail<=512, reply<=4096+flag, steps<=24)")
