#!/usr/bin/env python3
"""Live acceptance for t_c37aa0dc: a cleared injection does not mute the node.

Run this ON the controller host (stdlib only, like `solvable-faults-live.py`). It
injects a 60 s OOM fault on a REAL node, waits until the injected series has
really been written for most of its TTL, then clears it and answers ONE question
with raw rows:

    how soon after the clear is the node's OWN next sample stored?

Before the fix (measured 2026-10-17 on node-c): 2 m 19 s, because the injector's
1 Hz `seq` range sat above the node's slower counter and `store.insert_samples`
dropped every real sample until the node caught up. After the fix each source has
its own channel (`telemetry_samples.source`), so the node's next sample is stored
on its first POST after the recovery burst closes.

The clear is phase-aligned to the node's own POST cadence (observed in
`telemetry_batches`, which the controller still records while an injection
supersedes the samples), so the number is the STORE's latency, not an artefact of
when the operator happened to press the button.

    python3 infra/gcp/evidence/tools/demo-fault-clear-telemetry.py run \
        --node 01M4ZQ7Q2J80ATY3K32PY3PPJH --duration 60

Every line is a raw row; nothing is summarised away.
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
NODE_SOURCE = "node"
FAULT_SOURCE = "demo_fault"
# Who this tool says it is in `X-Termini-Actor` (kanban t_9c1362f6). Without it the
# controller records `cleared_by=unattributed_project_token`, which is the honest
# answer but names nobody; pass `--actor t_<card>` (or TERMINI_ACTOR) to name the card
# this run is evidence for.
DEFAULT_ACTOR = "tool:demo-fault-clear-telemetry"
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
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def pct(sample: dict) -> float:
    total = sample.get("ram_total_bytes") or 1
    return round(100.0 * (sample.get("ram_used_bytes") or 0) / total, 2)


def epoch(ts: str | None) -> float:
    if not ts:
        return 0.0
    from datetime import datetime, timezone

    return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(
        tzinfo=timezone.utc).timestamp()


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + \
        ".%03dZ" % int((t % 1) * 1000)


def sample_rows(db: str, node: str, source: str, since_ts: str) -> list[dict]:
    out = []
    for r in rows(db, "select source, seq, ts, ts_epoch, payload_json from telemetry_samples"
                      " where node_id=? and source=? and ts >= ? order by ts_epoch, seq",
                 (node, source, since_ts)):
        s = json.loads(r["payload_json"])
        out.append({
            "source": r["source"], "seq": r["seq"], "ts": r["ts"],
            "ram_pct": pct(s), "cpu_util_pct": s.get("cpu_util_pct"),
            "lxd_instances": s.get("lxd_instances"),
        })
    return out


# ------------------------------------------------------------------- the run
def do_run(args) -> int:
    node = args.node
    print("=== PRE-STATE %s ===" % node)
    pre = rows(args.db, "select source, seq, ts, payload_json from telemetry_samples"
                        " where node_id=? and source=? order by ts_epoch desc limit 1",
               (node, NODE_SOURCE))
    if pre:
        s = json.loads(pre[0]["payload_json"])
        print(json.dumps({"stored_head": {"source": pre[0]["source"],
                                          "seq": pre[0]["seq"], "ts": pre[0]["ts"],
                                          "ram_pct": pct(s),
                                          "lxd_instances": s.get("lxd_instances")}},
                         default=str))
    else:
        print(json.dumps({"stored_head": None}))
    head = rows(args.db, "select coalesce(max(id), 0) as id from telemetry_samples"
                         " where node_id=? and source=?", (node, NODE_SOURCE))[0]["id"]
    print(json.dumps({"node_channel_max_id": head}))

    print("\n=== POST /v1/projects/%s/demo/faults (scenario=%s duration=%.0fs) ==="
          % (args.project, args.scenario, args.duration))
    body = {"scenario": args.scenario, "node_id": node, "target": "real",
            "duration_s": float(args.duration)}
    status, resp = call(args.base, args.project, args.token, "POST",
                        "/v1/projects/%s/demo/faults" % args.project, body)
    print("HTTP", status)
    print(json.dumps(resp, indent=2, default=str) if isinstance(resp, (dict, list)) else resp)
    if status != 201:
        print("INJECTION REFUSED — nothing was injected")
        return 1
    injection_id = resp["injection_id"] if isinstance(resp, dict) else None
    print("injection_id:", injection_id)

    # Let the fault hold the series for most of its TTL — this is the condition
    # that used to leave the node tens of seqs behind. Abort if somebody else
    # closes the injection first (a peer's clear-all makes the whole run moot).
    print("\n=== holding the fault ===")
    deadline = time.time() + float(args.duration) + 5.0
    injected = 0
    state = "active"
    while time.time() < deadline:
        injected = rows(args.db, "select count(*) as n from telemetry_samples"
                                " where node_id=? and source=? and ts >= ?",
                        (node, FAULT_SOURCE, resp.get("started_at")))[0]["n"]
        st = rows(args.db, "select state, cleared_by from demo_faults where injection_id=?",
                  (injection_id,))
        state = st[0]["state"] if st else "missing"
        if state not in ("active", "recovering"):
            print(json.dumps({"injected_samples_written": injected,
                              "injection_state": state,
                              "cleared_by": st[0]["cleared_by"] if st else None,
                              "abort": "another actor closed the injection during the hold — "
                                       "the window is not ours, rerun",
                              "still_open_note": "cleared_by=human is the DEFAULT for any "
                                                 "caller, including a peer worker"}))
            return 3
        if injected >= args.hold_samples:
            break
        time.sleep(0.5)
    print(json.dumps({"injected_samples_written": injected, "injection_state": state}))

    # Phase-align the clear to the node's own report cadence: the supersede path
    # still records a telemetry_batches row for every POST the node makes.
    last_batch = rows(args.db, "select coalesce(max(ts_received), '') as t"
                               " from telemetry_batches where node_id=?", (node,))[0]["t"]
    print(json.dumps({"last_node_post_before_clear": last_batch}))
    print("waiting for the node's next POST …")
    wait_until = time.time() + args.post_wait
    while time.time() < wait_until:
        cur = rows(args.db, "select coalesce(max(ts_received), '') as t"
                            " from telemetry_batches where node_id=?", (node,))[0]["t"]
        if cur and cur != last_batch:
            print(json.dumps({"node_just_posted_at": cur}))
            break
        time.sleep(0.05)
    else:
        print("(no new node POST observed; clearing anyway)")

    cleared_req = time.time()
    # Head INSIDE the node's channel at the instant of the clear: the first row
    # with a greater id is by definition the first sample stored after it. (Using
    # the pre-injection head would have picked a sample stored during the hold.)
    head_at_clear = rows(args.db, "select coalesce(max(id), 0) as id from telemetry_samples"
                                  " where node_id=? and source=?", (node, NODE_SOURCE))[0]["id"]
    print("\n=== DELETE /v1/projects/%s/demo/faults/%s  at %s (node-channel head id %d) ==="
          % (args.project, injection_id, iso(cleared_req), head_at_clear))
    status, cresp = call(args.base, args.project, args.token, "DELETE",
                         "/v1/projects/%s/demo/faults/%s" % (args.project, injection_id))
    print("HTTP", status)
    print(json.dumps(cresp, indent=2, default=str) if isinstance(cresp, (dict, list)) else cresp)
    if status != 200:
        return 1
    if isinstance(cresp, dict) and cresp.get("status", "").startswith("already_"):
        print(json.dumps({"abort": "the injection was already closed by another actor "
                                   "(%s) — this window is not ours, rerun"
                                   % cresp.get("status")}))
        return 3

    # The injection stops superseding the node only when the recovery burst
    # finishes; watch the row state, then watch storage.
    t_closed = None
    first_stored = None
    observed = None
    deadline = cleared_req + args.observe_s
    while time.time() < deadline:
        if t_closed is None:
            st = rows(args.db, "select state, recovery_samples, samples_written, cleared_at"
                               " from demo_faults where injection_id=?", (injection_id,))
            if st and st[0]["state"] == "cleared":
                t_closed = time.time()
                print(json.dumps({"recovery_closed": {"at": iso(t_closed),
                                                      **st[0]}}, default=str))
        got = rows(args.db, "select id, source, seq, ts, ts_epoch, payload_json"
                            " from telemetry_samples where node_id=? and source=?"
                            " and id > ? order by id limit 1",
                   (node, NODE_SOURCE, head_at_clear))
        if got:
            observed = time.time()
            first_stored = got[0]
            break
        time.sleep(0.05)
    if first_stored is None:
        print(json.dumps({"first_node_sample_after_clear": None,
                          "observed_within_s": args.observe_s}))
        return 2

    s = json.loads(first_stored["payload_json"])
    print("\n=== THE NODE'S OWN SAMPLE, STORED AFTER THE CLEAR (raw row) ===")
    print(json.dumps({
        "id": first_stored["id"], "source": first_stored["source"],
        "seq": first_stored["seq"], "ts": first_stored["ts"],
        "ram_pct": pct(s), "ram_used_bytes": s.get("ram_used_bytes"),
        "ram_total_bytes": s.get("ram_total_bytes"),
        "cpu_util_pct": s.get("cpu_util_pct"),
        "lxd_instances": s.get("lxd_instances"),
        "hostname": s.get("hostname"),
    }, default=str))

    print("\n=== LATENCY ===")
    print(json.dumps({
        "clear_requested_at": iso(cleared_req),
        "recovery_closed_at": iso(t_closed) if t_closed else None,
        "node_sample_ts": first_stored["ts"],
        "node_sample_observed_stored_at": iso(observed),
        "seconds_clear_to_stored": round(observed - cleared_req, 3),
        "seconds_recovery_closed_to_stored": round(
            observed - t_closed, 3) if t_closed else None,
        "sample_ts_is_after_clear": epoch(first_stored["ts"]) > epoch(iso(cleared_req)),
        "sample_ts_is_after_recovery_closed": (
            epoch(first_stored["ts"]) > epoch(iso(t_closed)) if t_closed else None),
        # Node clock vs controller clock: the node's `ts` is what it stamped, the
        # observation time is when the controller stored it. A small negative
        # offset is skew, not a late sample.
        "node_clock_minus_controller_at_store_s": round(
            epoch(first_stored["ts"]) - observed, 3),
    }))

    print("\n=== STORED SERIES SINCE THE INJECTION (both channels, raw rows) ===")
    since = resp.get("started_at")
    for row in sample_rows(args.db, node, FAULT_SOURCE, since):
        print(json.dumps(row, default=str)[:600])
    for row in sample_rows(args.db, node, NODE_SOURCE, since):
        print(json.dumps(row, default=str)[:600])

    print("\n=== demo_faults / operator_logs / trigger_state ===")
    for r in rows(args.db, "select injection_id, scenario, state, target, duration_s,"
                           " started_at, cleared_at, cleared_by, clear_reason,"
                           " samples_written, recovery_samples from demo_faults"
                           " where injection_id=?", (injection_id,)):
        print(json.dumps(r, default=str))
    for r in rows(args.db, "select ts, severity, actor, message from operator_logs"
                           " where node_id=? and ts >= ? order by ts", (node, since)):
        print(json.dumps(r, default=str)[:800])
    for r in rows(args.db, "select rule_id, state, fired_count, last_fired_at,"
                           " last_cleared_at from trigger_state where node_id=?"
                           " order by rule_id", (node,)):
        print(json.dumps(r, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default=DEFAULT_BASE)
    p.add_argument("--project", default=DEFAULT_PID)
    p.add_argument("--token", default="demo-project-token")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--actor", default=os.environ.get("TERMINI_ACTOR", DEFAULT_ACTOR),
                   help="value for X-Termini-Actor (default: %(default)s); pass the "
                        "card id this run is evidence for")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="inject 60 s, phase-align, clear, observe storage")
    r.add_argument("--node", required=True)
    r.add_argument("--scenario", default="oom", choices=("oom", "disk-fail"))
    r.add_argument("--duration", type=float, default=60.0)
    r.add_argument("--hold-samples", type=int, default=50,
                   help="clear once this many injected samples exist (1 Hz)")
    r.add_argument("--post-wait", type=float, default=20.0,
                   help="max seconds to wait for the node's next POST")
    r.add_argument("--observe-s", type=float, default=30.0,
                   help="max seconds to watch for the node's stored sample")
    r.add_argument("--actor", dest="actor", default=argparse.SUPPRESS,
                   help=argparse.SUPPRESS)
    r.set_defaults(func=do_run)
    args = p.parse_args(argv)
    global ACTOR
    ACTOR = args.actor
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
