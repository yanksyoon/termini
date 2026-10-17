#!/usr/bin/env python3
"""Live half of the t_895bf6d3 acceptance: the injected fault is SOLVED.

Run this ON the controller host (stdlib only; the demo fault console is a
loopback endpoint authenticated with the project token, and the rows read below
live in the controller's own SQLite file). It is the companion to
`solvable-faults-hermetic.py`, which proves the same loop hermetically.

    # inject the demo OOM signature on a REAL node for 60 s
    python3 infra/gcp/evidence/tools/solvable-faults-live.py inject \
        --node 01M4ZQ7Q2J80ATY3K32PY3PPJH --duration 60

    # what the loop did about it (samples, decision, audit, logs, rule state)
    python3 infra/gcp/evidence/tools/solvable-faults-live.py status \
        --since 2026-10-17T18:44:00Z

    # clear it and watch the recovery
    python3 infra/gcp/evidence/tools/solvable-faults-live.py clear \
        --injection 01M55JV7NHP5K1A42VCN6PS1F3

Every command prints raw rows; nothing is summarised, so the transcript is the
evidence. `inject` waits for the decision+audit and exits when both exist (or
after `--wait`), which keeps the node-side observation (a PID/uptime change) in
the same few seconds as the LXD call.

What this canNOT show on its own: the node-side state change. Pair it with, on
the node the executor dials:

    sudo lxc info <instance> | grep -E 'Status|PID'
    sudo lxc exec <instance> -- cat /proc/uptime

before and after the decision. A target that does not exist cannot produce that
pair, which is the point of the card.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8080"
DEFAULT_PID = "01J0DEM0WARM000000000000AB"
DEFAULT_DB = "/data/controller.db"
# Who this tool says it is in `X-Termini-Actor` (kanban t_9c1362f6) — see
# demo-fault-clear-telemetry.py for why the header exists.
DEFAULT_ACTOR = "tool:solvable-faults-live"
ACTOR = DEFAULT_ACTOR


# --------------------------------------------------------------------- plumbing
def call(base: str, project: str, token: str, method: str, path: str,
         body: dict | None = None, timeout: float = 30.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base.rstrip("/") + path, data=data, method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                 "X-Termini-Actor": ACTOR},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw.decode(errors="replace")


def rows(db_path: str, sql: str, args: tuple = ()):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def pct(sample: dict) -> float:
    total = sample.get("ram_total_bytes") or 1
    return round(100.0 * (sample.get("ram_used_bytes") or 0) / total, 2)


# ---------------------------------------------------------------------- actions
def do_inject(args) -> int:
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print("=== PRE-STATE (latest stored sample of %s) ===" % args.node)
    latest = rows(args.db, "select seq, ts, payload_json from telemetry_samples"
                            " where node_id=? order by seq desc limit 1", (args.node,))
    if latest:
        s = json.loads(latest[0]["payload_json"])
        print(json.dumps({"seq": latest[0]["seq"], "ts": latest[0]["ts"],
                          "ram_pct": pct(s), "cpu_util_pct": s.get("cpu_util_pct"),
                          "lxd_instances": s.get("lxd_instances")}, default=str))
    else:
        print("(node has reported no sample yet)")

    print("\n=== POST %s/v1/projects/%s/demo/faults ===" % (args.base, args.project))
    body = {"scenario": args.scenario, "node_id": args.node,
            "target": args.target, "duration_s": args.duration}
    status, resp = call(args.base, args.project, args.token, "POST",
                        "/v1/projects/%s/demo/faults" % args.project, body)
    print("HTTP", status)
    print(json.dumps(resp, indent=2, default=str) if isinstance(resp, (dict, list)) else resp)
    if status != 201:
        print("INJECTION REFUSED — nothing was injected")
        return 1
    injection_id = resp.get("injection_id") if isinstance(resp, dict) else None
    print("\ninjection_id:", injection_id, " requested_at:", started)

    print("\n=== WAITING for the decision + audit (up to %.0f s) ===" % args.wait)
    deadline = time.time() + args.wait
    while time.time() < deadline:
        d = rows(args.db, "select id from remediation_decisions where node_id=?"
                          " and decided_at >= ? order by decided_at desc limit 1",
                 (args.node, started))
        if d:
            a = rows(args.db, "select result from remediation_audits where decision_id=?",
                     (d[0]["id"],))
            if a:
                print("decision+audit present after %.1f s — run `status --since %s`"
                      % (time.time() - (deadline - args.wait), started))
                return 0
        time.sleep(1)
    print("no decision+audit within %.0f s" % args.wait)
    return 2


def do_clear(args) -> int:
    status, resp = call(args.base, args.project, args.token, "POST",
                        "/v1/projects/%s/demo/faults/clear" % args.project,
                        {"injection_id": args.injection})
    print("HTTP", status)
    print(json.dumps(resp, indent=2, default=str) if isinstance(resp, (dict, list)) else resp)
    return 0 if status == 200 else 1


def do_status(args) -> int:
    node, since = args.node, args.since
    print("--- samples (%s) since %s ---" % (node, since))
    for r in rows(args.db, "select seq, ts, payload_json from telemetry_samples"
                           " where node_id=? and ts >= ? order by seq", (node, since)):
        s = json.loads(r["payload_json"])
        print(json.dumps({"seq": r["seq"], "ts": r["ts"], "ram_pct": pct(s)}))
    print("--- decisions ---")
    for r in rows(args.db, "select id, rule_id, action_kind, action_params, decided_at,"
                           " decision_source, confidence, fallback_reason, incident_title"
                           " from remediation_decisions where node_id=? and decided_at >= ?"
                           " order by decided_at", (node, since)):
        print(json.dumps(r, default=str))
    print("--- audits ---")
    for r in rows(args.db, "select id, decision_id, rule_id, action_kind, result, detail,"
                           " lxd_method, lxd_path, lxd_body, lxd_endpoint, lxd_operation,"
                           " executed_at, reason from remediation_audits"
                           " where node_id=? and executed_at >= ? order by executed_at",
                  (node, since)):
        print(json.dumps(r, default=str))
    print("--- demo_faults ---")
    for r in rows(args.db, "select injection_id, scenario, state, target, started_at,"
                           " cleared_at, samples_written, recovery_samples, clear_reason"
                           " from demo_faults where node_id=? order by started_at desc limit 5",
                  (node,)):
        print(json.dumps(r, default=str))
    print("--- trigger_state ---")
    for r in rows(args.db, "select rule_id, state, fired_count, last_fired_at, last_cleared_at"
                           " from trigger_state where node_id=? order by rule_id", (node,)):
        print(json.dumps(r, default=str))
    print("--- operator_logs ---")
    for r in rows(args.db, "select ts, severity, actor, message from operator_logs"
                           " where node_id=? and ts >= ? order by ts", (node, since)):
        print(json.dumps(r, default=str)[:800])
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default=DEFAULT_BASE, help="controller base URL (loopback)")
    p.add_argument("--project", default=DEFAULT_PID)
    p.add_argument("--token", default="demo-project-token")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--actor", default=os.environ.get("TERMINI_ACTOR", DEFAULT_ACTOR),
                   help="value for X-Termini-Actor (default: %(default)s)")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("inject", help="inject a demo fault on a real node and wait for the loop")
    i.add_argument("--node", required=True)
    i.add_argument("--scenario", default="oom", choices=("oom", "disk-fail"))
    i.add_argument("--target", default="real", choices=("real", "synthetic"))
    i.add_argument("--duration", type=float, default=60.0)
    i.add_argument("--wait", type=float, default=150.0)
    i.add_argument("--actor", dest="actor", default=argparse.SUPPRESS,
                   help=argparse.SUPPRESS)
    i.set_defaults(func=do_inject)

    c = sub.add_parser("clear", help="clear an injection and let the series recover")
    c.add_argument("--injection", required=True)
    c.add_argument("--actor", dest="actor", default=argparse.SUPPRESS,
                   help=argparse.SUPPRESS)
    c.set_defaults(func=do_clear)

    s = sub.add_parser("status", help="print the rows the loop produced")
    s.add_argument("--node", required=True)
    s.add_argument("--since", default="1970-01-01T00:00:00Z")
    s.set_defaults(func=do_status)

    args = p.parse_args(argv)
    global ACTOR
    ACTOR = args.actor
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
