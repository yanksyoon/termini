#!/usr/bin/env python3
"""Collect the controller-side evidence for t_f466824c (runs ON the controller VM).

Prints RAW output only: the operator's own log lines, the decisions table (which
carries `decision_source`) and the audit rows. No summarising, no re-typing.
"""
from __future__ import annotations

import glob
import os
import re
import sqlite3
import subprocess
import sys


def run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    except Exception as exc:  # noqa: BLE001
        return f"(command failed: {type(exc).__name__}: {exc})"


def find_db() -> str:
    env = run(["systemctl", "show", "termini-controller", "-p", "Environment"])
    for token in env.split():
        if "DB_PATH" in token and "=" in token:
            return token.split("=", 1)[1]
    for pattern in ("/var/lib/termini/*.db", "/opt/termini/*.db", "/home/ubuntu/*.db",
                    "/var/lib/termini/*.sqlite*"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
    hits = [p for p in glob.glob("/opt/termini/**/*", recursive=True)
            if p.endswith((".db", ".sqlite", ".sqlite3"))]
    return hits[0] if hits else ""


DB = find_db()
print(f"### controller DB: {DB or '(not found)'}")
print(f"### controller unit env (inference/operator keys)")
env = run(["systemctl", "show", "termini-controller", "-p", "Environment"])
for token in env.split():
    if re.search(r"(INFERENCE|OPERATOR|DECISION)", token):
        print(f"    {token}")

print("\n### journalctl -u termini-controller (last 400 lines, filtered)")
journal = run(["journalctl", "-u", "termini-controller", "--no-pager", "-n", "400"])
keys = ("externalModelCall", "FIRED", "fallback", "decision", "egress", "operator.loop")
for line in journal.splitlines():
    if any(k in line for k in keys):
        print(line)

if not DB:
    sys.exit(0)

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
print("\n### remediation_decisions (latest 5): id/rule/decision_source/confidence/fallback/decided_at")
try:
    for row in conn.execute(
        "SELECT id, node_id, rule_id, action_kind, decision_source, confidence,"
        " fallback_reason, decided_at FROM remediation_decisions"
        " ORDER BY decided_at DESC LIMIT 5"
    ):
        print(dict(row))
except Exception as exc:  # noqa: BLE001
    print(f"(query failed: {exc})")

print("\n### remediation_audits (latest 5): result/lxd_endpoint/executed_at")
try:
    for row in conn.execute(
        "SELECT id, decision_id, node_id, rule_id, action_kind, result, lxd_endpoint,"
        " executed_at FROM remediation_audits ORDER BY executed_at DESC LIMIT 5"
    ):
        print(dict(row))
except Exception as exc:  # noqa: BLE001
    print(f"(query failed: {exc})")

print("\n### operator log stream rows mentioning the model call / destination (latest 12)")
try:
    for row in conn.execute(
        "SELECT log_id, ts, severity, actor, message FROM operator_logs"
        " WHERE message LIKE '%externalModelCall%' OR message LIKE '%destination=%'"
        " ORDER BY ts DESC LIMIT 12"
    ):
        print(dict(row))
except Exception as exc:  # noqa: BLE001
    print(f"(query failed: {exc})")

print("\n### tables present")
try:
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print(names)
except Exception as exc:  # noqa: BLE001
    print(f"(query failed: {exc})")
print(f"\n### cwd={os.getcwd()}")
