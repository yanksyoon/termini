#!/usr/bin/env python3
"""Live half of the t_77f26781 acceptance: consecutive model decisions.

Injects the demo OOM signature N times on a REAL node of the standing fleet and
prints, for every fire, the decision row the loop wrote plus the decision-trace
steps that explain how the decision was reached (which model replied, what it
said, whether the reply had to be repaired, what was executed).

Run this ON the controller host (stdlib only; the demo fault console is a
loopback endpoint authenticated with the project token, and the rows read below
live in the controller's own SQLite file):

    python3 infra/gcp/evidence/tools/model-decisions-live.py run \
        --node 01M4TGD31WXH97JAA5S6A4R10D --cycles 6

    # just read back what the last run produced
    python3 infra/gcp/evidence/tools/model-decisions-live.py report \
        --node 01M4TGD31WXH97JAA5S6A4R10D --since 2026-10-17T20:00:00Z

Every command prints raw rows; nothing is summarised away, so the transcript is
the evidence. `run` injects one fault at a time, waits for the decision AND the
audit, clears the injection, and waits for the rule to re-arm before the next
cycle — consecutive fires, not one fire and a cooldown.

What this canNOT show on its own: the node-side effect of the action (a
container's PID/uptime resetting). Pair it with, on the node the executor dials:

    sudo lxc info <instance> | grep -E 'Status|PID'
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8080"
DEFAULT_PID = "01J0DEM0WARM000000000000AB"
DEFAULT_DB = "/data/controller.db"
DEFAULT_TOKEN = "demo-project-token"

# The rules the demo fault console can drive, per scenario.
SCENARIO_RULES = {"oom": "oom-guard-01", "disk-fail": "disk-fail-01"}


# --------------------------------------------------------------------- plumbing
def call(base: str, project: str, token: str, method: str, path: str,
         body: dict | None = None, timeout: float = 30.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base.rstrip("/") + path, data=data, method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else {}
        except ValueError:
            return exc.code, {"raw": raw.decode("utf-8", "replace")}


def rows(db_path: str, sql: str, args: tuple = ()) -> list:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def trace_steps(decision_id: str, db_path: str) -> list:
    out = rows(db_path, "select decision_trace from remediation_decisions where id=?",
               (decision_id,))
    if not out or not out[0]["decision_trace"]:
        return []
    return json.loads(out[0]["decision_trace"]).get("steps") or []


def print_decision(decision_id: str, db_path: str, *, full_reply: bool = False) -> dict:
    dec = rows(db_path,
               "select id, node_id, rule_id, decided_at, decision_source, confidence,"
               " fallback_reason, action_kind, action_params, incident_title,"
               " rationale from remediation_decisions where id=?", (decision_id,))
    if not dec:
        print("  (no decision row %s)" % decision_id)
        return {}
    d = dec[0]
    print("  decision:", json.dumps(d, default=str))
    audits = rows(db_path,
                  "select id, result, detail, lxd_method, lxd_path, lxd_body,"
                  " lxd_endpoint from remediation_audits where decision_id=?", (decision_id,))
    for a in audits:
        print("  audit:   ", json.dumps(a, default=str))
    for step in trace_steps(decision_id, db_path):
        detail = str(step.get("detail", ""))
        if step.get("step") == "reply_received" and not full_reply:
            detail = detail[:400] + ("…" if len(detail) > 400 else "")
        print("  trace: %-14s state=%-8s %s" % (step.get("step"), step.get("state"), detail))
    return d


# ------------------------------------------------------------------- commands
def wait_for_decision(node: str, since: str, db_path: str, wait_s: float) -> str | None:
    deadline = time.time() + wait_s
    while time.time() < deadline:
        d = rows(db_path,
                 "select id from remediation_decisions where node_id=? and decided_at >= ?"
                 " order by decided_at limit 1", (node, since))
        if d:
            return d[0]["id"]
        time.sleep(1)
    return None


def wait_for_armed(node: str, rule_id: str, db_path: str, wait_s: float) -> bool:
    deadline = time.time() + wait_s
    while time.time() < deadline:
        st = rows(db_path,
                  "select state from trigger_state where node_id=? and rule_id=?",
                  (node, rule_id))
        if st and st[0]["state"] == "armed":
            return True
        time.sleep(1)
    return False


def do_run(args) -> int:
    rule_id = SCENARIO_RULES.get(args.scenario, "oom-guard-01")
    print("=== run: node=%s scenario=%s cycles=%d at %s ==="
          % (args.node, args.scenario, args.cycles, utcnow()))
    results = []
    for cycle in range(1, args.cycles + 1):
        started = utcnow()
        print("\n--- cycle %d/%d: inject at %s ---" % (cycle, args.cycles, started))
        status, resp = call(args.base, args.project, args.token, "POST",
                            "/v1/projects/%s/demo/faults" % args.project,
                            {"scenario": args.scenario, "node_id": args.node,
                             "target": args.target, "duration_s": args.duration})
        print("  HTTP", status, json.dumps(resp, default=str)[:400])
        if status != 201:
            print("  INJECTION REFUSED — stopping")
            return 1
        injection = resp.get("injection_id") if isinstance(resp, dict) else None

        decision_id = wait_for_decision(args.node, started, args.db, args.wait)
        if decision_id is None:
            print("  no decision within %.0f s" % args.wait)
            results.append({"cycle": cycle, "injection": injection, "decision": None})
        else:
            d = print_decision(decision_id, args.db, full_reply=args.full_reply)
            results.append({"cycle": cycle, "injection": injection,
                            "decision": decision_id,
                            "source": d.get("decision_source"),
                            "confidence": d.get("confidence"),
                            "fallback_reason": d.get("fallback_reason"),
                            "action": d.get("action_kind")})

        if injection and not args.keep:
            status, resp = call(args.base, args.project, args.token, "POST",
                                "/v1/projects/%s/demo/faults/clear" % args.project,
                                {"injection_id": injection})
            print("  clear HTTP", status, json.dumps(resp, default=str)[:200])
        if cycle < args.cycles:
            armed = wait_for_armed(args.node, rule_id, args.db, args.rearm_wait)
            print("  %s %s before the next fire"
                  % (rule_id, "armed" if armed else "DID NOT re-arm in time"))

    print("\n=== summary ===")
    for r in results:
        print(json.dumps(r, default=str))
    model = [r for r in results if r.get("source") == "model"]
    print("\n%d/%d fires produced decision_source=model" % (len(model), len(results)))
    return 0 if len(model) == len(results) == args.cycles else 2


def do_report(args) -> int:
    print("=== decisions since %s on %s ===" % (args.since, args.node))
    decs = rows(args.db,
                "select id, decided_at, rule_id, decision_source, confidence,"
                " fallback_reason, action_kind, incident_title, rationale"
                " from remediation_decisions where node_id=? and decided_at >= ?"
                " order by decided_at", (args.node, args.since))
    for d in decs:
        print("\n--", d["id"], d["decided_at"], d["rule_id"])
        print_decision(d["id"], args.db, full_reply=args.full_reply)
    print("\n=== raw rows ===")
    print(json.dumps(decs, indent=2, default=str))
    return 0


def do_state(args) -> int:
    print("=== trigger_state on %s ===" % args.node)
    for r in rows(args.db, "select rule_id, state, fired_count, last_fired_at,"
                           " last_cleared_at from trigger_state where node_id=?"
                           " order by rule_id", (args.node,)):
        print(json.dumps(r, default=str))
    print("\n=== latest sample ===")
    s = rows(args.db, "select seq, ts, payload_json from telemetry_samples where node_id=?"
                      " order by seq desc limit 1", (args.node,))
    if s:
        p = json.loads(s[0]["payload_json"])
        print(json.dumps({"seq": s[0]["seq"], "ts": s[0]["ts"],
                          "ram_used_bytes": p.get("ram_used_bytes"),
                          "ram_total_bytes": p.get("ram_total_bytes"),
                          "lxd_instances": p.get("lxd_instances")}, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default=DEFAULT_BASE, help="controller base URL (loopback)")
    p.add_argument("--project", default=DEFAULT_PID)
    p.add_argument("--token", default=DEFAULT_TOKEN)
    p.add_argument("--db", default=DEFAULT_DB)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="inject N consecutive faults and print each decision")
    r.add_argument("--node", required=True)
    r.add_argument("--scenario", default="oom", choices=sorted(SCENARIO_RULES))
    r.add_argument("--target", default="real", choices=("real", "synthetic"))
    r.add_argument("--cycles", type=int, default=6)
    r.add_argument("--duration", type=float, default=30.0)
    r.add_argument("--wait", type=float, default=150.0, help="wait per decision")
    r.add_argument("--rearm-wait", type=float, default=150.0,
                   help="wait for the rule to re-arm between fires")
    r.add_argument("--keep", action="store_true", help="do not clear the injection")
    r.add_argument("--full-reply", action="store_true")
    r.set_defaults(func=do_run)

    rep = sub.add_parser("report", help="print the decisions a run produced")
    rep.add_argument("--node", required=True)
    rep.add_argument("--since", required=True)
    rep.add_argument("--full-reply", action="store_true")
    rep.set_defaults(func=do_report)

    st = sub.add_parser("state", help="print the node's rule state and latest sample")
    st.add_argument("--node", required=True)
    st.set_defaults(func=do_state)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
