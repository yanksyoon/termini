#!/usr/bin/env python3
"""Collect the MODEL-MODE decision evidence ON the controller (t_f466824c).

Prints raw output only. Three things matter for acceptance (b):
  * the decision row itself (`decision_source=model`),
  * the `externalModelCall` audit pair that brackets the forwarded call — the
    audit sink rows in `operator_logs` AND the journald lines, whose `before` and
    `after` timestamps give the forwarded call's wall clock in SECONDS,
  * the external destination, so no reader can mistake the answer for local
    weights.

Usage: python3 collect-model-decision.py [--since ISO8601]
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
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
    for pattern in ("/var/lib/termini/*.db", "/opt/termini/*.db", "/home/ubuntu/*.db"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
    return ""


def parse_journal_ts(line: str):
    """`Oct 15 10:47:09 host python3[1]: 2026-10-15 10:47:09,357 INFO ...` -> datetime."""
    match = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})", line)
    if not match:
        return None
    try:
        return dt.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="", help="only rows at/after this RFC3339 ts")
    ap.add_argument("--limit", type=int, default=8)
    args = ap.parse_args()

    db = find_db()
    print(f"### controller DB: {db or '(not found)'}")
    if not db:
        return 1

    print("\n### operator unit env (decision mode + inference endpoint)")
    env = run(["systemctl", "show", "termini-controller", "-p", "Environment"])
    for token in env.split():
        if re.search(r"(INFERENCE|OPERATOR|DECISION)", token):
            print(f"    {token}")

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    where = " WHERE decided_at >= ?" if args.since else ""
    params = [args.since] if args.since else []
    print(f"\n### remediation_decisions (latest {args.limit}) — ALL columns")
    try:
        rows = list(conn.execute(
            f"SELECT * FROM remediation_decisions{where}"
            f" ORDER BY decided_at DESC LIMIT {int(args.limit)}", params))
        cols = [d[0] for d in conn.execute("SELECT * FROM remediation_decisions LIMIT 1").description]
        print(f"# columns: {cols}")
        print("# NOTE: this table has NO latency column — the forwarded call's wall clock is")
        print("# measured from the externalModelCall before/after audit pair below.")
        for row in rows:
            print(dict(row))
    except Exception as exc:  # noqa: BLE001
        print(f"(query failed: {exc})")

    print(f"\n### operator_logs: externalModelCall rows (latest {args.limit * 2})")
    try:
        if args.since:
            q = ("SELECT log_id, ts, severity, actor, message FROM operator_logs"
                 " WHERE message LIKE '%externalModelCall%' AND ts >= ?"
                 " ORDER BY ts DESC LIMIT ?")
            rows = list(conn.execute(q, (args.since, args.limit * 2)))
        else:
            q = ("SELECT log_id, ts, severity, actor, message FROM operator_logs"
                 " WHERE message LIKE '%externalModelCall%' ORDER BY ts DESC LIMIT ?")
            rows = list(conn.execute(q, (args.limit * 2,)))
        if not rows:
            print("(no externalModelCall row — the decision did not go through the guard)")
        for row in rows:
            print(dict(row))
    except Exception as exc:  # noqa: BLE001
        print(f"(query failed: {exc})")

    print("\n### journald: externalModelCall before/after pairs + the forwarded call's latency")
    journal = run(["journalctl", "-u", "termini-controller", "--no-pager", "-n", "2000",
                   "--since", "-30min"])
    events: dict[str, dict] = {}
    for line in journal.splitlines():
        if "externalModelCall" not in line:
            continue
        phase = "before" if "externalModelCall before" in line else (
            "after" if "externalModelCall after" in line else "")
        if not phase:
            continue
        ts = parse_journal_ts(line)
        rid = ""
        match = re.search(r"'request_id': '([^']+)'", line)
        if match:
            rid = match.group(1)
        events.setdefault(rid, {})[phase] = (ts, line)

    if not events:
        print("(no externalModelCall lines in the last 30 min)")
    for rid, phases in events.items():
        before, after = phases.get("before"), phases.get("after")
        if before and after and before[0] and after[0]:
            secs = (after[0] - before[0]).total_seconds()
            print(f"\n### request_id={rid}: forwarded call wall clock = {secs:.3f} s")
            print(f"    before: {before[1].strip()[:400]}")
            print(f"    after:  {after[1].strip()[:400]}")
        else:
            for phase, (ts, line) in phases.items():
                print(f"\n### request_id={rid} [{phase} only, no pair]: {line.strip()[:400]}")

    print("\n### decision_fallback_state (the per-(node,rule) fallback ledger)")
    try:
        for row in conn.execute("SELECT * FROM decision_fallback_state"):
            print(dict(row))
    except Exception as exc:  # noqa: BLE001
        print(f"(query failed: {exc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
