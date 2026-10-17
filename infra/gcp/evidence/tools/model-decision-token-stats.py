#!/usr/bin/env python3
"""Completion-token distribution of the live model decisions (t_77f26781).

Reads the model_call step of every model decision's trace and prints how many
completion tokens each reply needed, so the truncation claim is measured against
the real replies rather than asserted.
"""
import json
import re
import sqlite3

DB = "/data/controller.db"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
counts = []
for row in conn.execute(
        "select id, decided_at, decision_trace from remediation_decisions"
        " where decision_source='model' and decision_trace is not null"
        " order by decided_at desc limit 60"):
    try:
        steps = json.loads(row["decision_trace"]).get("steps") or []
    except ValueError:
        continue
    for step in steps:
        if step.get("step") != "model_call":
            continue
        m = re.search(r"completion=(\d+)", str(step.get("detail", "")))
        if m:
            counts.append((row["decided_at"], int(m.group(1))))
            break
print("model decisions with a completion count:", len(counts))
for ts, n in counts:
    print(" ", ts, n)
over = [c for c in counts if c[1] > 512]
print("replies > 512 completion tokens:", len(over), "of", len(counts))
if counts:
    nums = sorted(c[1] for c in counts)
    print("min/median/max:", nums[0], nums[len(nums) // 2], nums[-1])
