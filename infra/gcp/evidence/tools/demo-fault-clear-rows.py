#!/usr/bin/env python3
"""Read back a demo-fault clear from the controller DB (raw rows, both channels).

Companion to `demo-fault-clear-telemetry.py`, which performs the live run. This
prints what the store holds afterwards, so a reader can check the two claims the
acceptance rests on:

  * the node's OWN stream continues after the clear, with its real RAM and its
    real `lxd_instances[]`, tagged `source='node'`;
  * the injected samples (fault + the recovery burst) are still there and are
    tagged `source='demo_fault'` — the provenance is per row.

Run ON the controller host:

    python3 infra/gcp/evidence/tools/demo-fault-clear-rows.py \
        --node 01M4ZQ7Q2J80ATY3K32PY3PPJH \
        --since 2026-10-17T19:48:40Z \
        --injection 01M55PHJC9PP6FRA4NPFRDJQFN
"""
from __future__ import annotations

import argparse
import json
import sqlite3

NODE = "01M4ZQ7Q2J80ATY3K32PY3PPJH"


def rows(db: str, sql: str, args: tuple = ()):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def pct(sample: dict) -> float:
    total = sample.get("ram_total_bytes") or 1
    return round(100.0 * (sample.get("ram_used_bytes") or 0) / total, 2)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="/data/controller.db")
    p.add_argument("--node", default=NODE)
    p.add_argument("--since", default="1970-01-01T00:00:00Z")
    p.add_argument("--injection", default=None)
    p.add_argument("--limit", type=int, default=8)
    args = p.parse_args(argv)

    print("== node-channel rows since %s (raw) ==" % args.since)
    for r in rows(args.db, "select id, source, seq, ts, payload_json from telemetry_samples"
                           " where node_id=? and source='node' and ts >= ?"
                           " order by id limit ?", (args.node, args.since, args.limit)):
        s = json.loads(r["payload_json"])
        print(json.dumps({"id": r["id"], "source": r["source"], "seq": r["seq"],
                          "ts": r["ts"], "ram_pct": pct(s),
                          "lxd_instances": s.get("lxd_instances"),
                          "hostname": s.get("hostname")}, default=str))

    print("== injected rows since %s (raw) ==" % args.since)
    for r in rows(args.db, "select id, source, seq, ts, payload_json from telemetry_samples"
                           " where node_id=? and source='demo_fault' and ts >= ?"
                           " order by ts limit 3", (args.node, args.since)):
        s = json.loads(r["payload_json"])
        print(json.dumps({"id": r["id"], "source": r["source"], "seq": r["seq"],
                          "ts": r["ts"], "ram_pct": pct(s)}, default=str))
    for r in rows(args.db, "select id, source, seq, ts, payload_json from telemetry_samples"
                           " where node_id=? and source='demo_fault' and ts >= ?"
                           " order by ts desc limit 3", (args.node, args.since)):
        s = json.loads(r["payload_json"])
        print(json.dumps({"id": r["id"], "source": r["source"], "seq": r["seq"],
                          "ts": r["ts"], "ram_pct": pct(s)}, default=str))

    print("== counts per source since %s ==" % args.since)
    for r in rows(args.db, "select source, count(*) n, min(ts) first_ts, max(ts) last_ts"
                           " from telemetry_samples where node_id=? and ts >= ?"
                           " group by source", (args.node, args.since)):
        print(json.dumps(r, default=str))

    if args.injection:
        print("== the injection row ==")
        for r in rows(args.db, "select injection_id, scenario, state, target, duration_s,"
                               " started_at, cleared_at, cleared_by, clear_reason,"
                               " samples_written, recovery_samples from demo_faults"
                               " where injection_id=?", (args.injection,)):
            print(json.dumps(r, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
