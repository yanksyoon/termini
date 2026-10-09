#!/usr/bin/env python3
"""demo_probe.py — small read-only probes for the demo dry run harness.

Subcommands (all stdlib):
  wait-audit   poll GET /v1/operator/audits until a rule's audit appears
  audits       print GET /v1/operator/audits
  events       print GET /v1/operator/events
  nodes        print GET /v1/projects/{pid}/nodes
  telemetry    print GET /v1/telemetry/recent
  sse-logs     stream GET /v1/projects/{pid}/logs (SSE) to a file for N seconds
  triggers     dump the operator trigger_config / trigger_state rows from a DB
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.request


def _get(base: str, path: str, token: str, timeout: float = 8.0):
    req = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode(errors="replace")


def _json(base, path, token):
    try:
        _, body = _get(base, path, token)
        return json.loads(body)
    except Exception:
        return {}


def cmd_wait_audit(a) -> int:
    deadline = time.monotonic() + a.timeout
    while time.monotonic() < deadline:
        data = _json(a.url, "/v1/operator/audits", a.token)
        for x in data.get("audits", []):
            if x.get("rule_id") == a.rule and (not a.expect_result or x.get("result") == a.expect_result):
                print(json.dumps(x))
                return 0
        time.sleep(0.25)
    print(json.dumps({"error": "timeout", "rule": a.rule}), file=sys.stderr)
    return 1


def cmd_audits(a) -> int:
    print(json.dumps(_json(a.url, "/v1/operator/audits", a.token), indent=2))
    return 0


def cmd_events(a) -> int:
    print(json.dumps(_json(a.url, "/v1/operator/events", a.token), indent=2))
    return 0


def cmd_nodes(a) -> int:
    print(json.dumps(_json(a.url, f"/v1/projects/{a.project}/nodes", a.token), indent=2))
    return 0


def cmd_telemetry(a) -> int:
    print(json.dumps(_json(a.url, f"/v1/telemetry/recent?node={a.node or ''}&limit={a.limit}", a.token), indent=2))
    return 0


def cmd_sse_logs(a) -> int:
    since = a.since or "00000000000000000000000000"
    url = f"{a.url}/v1/projects/{a.project}/logs?token={a.token}&since={since}"
    req = urllib.request.Request(url)
    end = time.monotonic() + a.seconds
    with open(a.out, "w", encoding="utf-8") as fh, urllib.request.urlopen(req, timeout=a.seconds + 3) as r:
        while time.monotonic() < end:
            line = r.readline()
            if not line:
                break
            fh.write(line.decode(errors="replace"))
            fh.flush()
    return 0


def cmd_inference(a) -> int:
    """Probe the inference node's resident llama.cpp server (demo Beat 3 path)."""
    ok = True
    for path in ("/health", "/v1/models"):
        try:
            with urllib.request.urlopen(a.url.rstrip("/") + path, timeout=a.timeout) as r:
                body = r.read().decode(errors="replace")
                print(f"{path} -> {r.status} {body[:160]}")
        except Exception as e:
            ok = False
            print(f"{path} -> {type(e).__name__}: {e}")
    return 0 if ok else 1


def cmd_triggers(a) -> int:
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    out = {"trigger_config": [], "trigger_state": []}
    for rid, cfg, src, upd in con.execute(
        "SELECT rule_id, config_json, source, updated_at FROM trigger_config ORDER BY rule_id"
    ):
        out["trigger_config"].append({"rule_id": rid, "source": src, "updated_at": upd,
                                      "config": json.loads(cfg)})
    for nid, rid, st, fired, cleared, cnt, upd in con.execute(
        "SELECT node_id, rule_id, state, last_fired_at, last_cleared_at, fired_count, updated_at"
        " FROM trigger_state ORDER BY node_id, rule_id"
    ):
        out["trigger_state"].append({"node_id": nid, "rule_id": rid, "state": st,
                                     "last_fired_at": fired, "last_cleared_at": cleared,
                                     "fired_count": cnt, "updated_at": upd})
    con.close()
    print(json.dumps(out, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:18085")
    ap.add_argument("--token", default="demo-project-token")
    ap.add_argument("--project", default="01J0DEM0WARM000000000000AB")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("wait-audit"); p.add_argument("--rule", required=True)
    p.add_argument("--expect-result", default="ok"); p.add_argument("--timeout", type=float, default=60.0)
    p.set_defaults(fn=cmd_wait_audit)

    p = sub.add_parser("audits"); p.set_defaults(fn=cmd_audits)
    p = sub.add_parser("events"); p.set_defaults(fn=cmd_events)
    p = sub.add_parser("nodes"); p.set_defaults(fn=cmd_nodes)
    p = sub.add_parser("telemetry"); p.add_argument("--node", default=""); p.add_argument("--limit", type=int, default=1)
    p.set_defaults(fn=cmd_telemetry)
    p = sub.add_parser("sse-logs"); p.add_argument("--out", required=True); p.add_argument("--seconds", type=float, default=8.0)
    p.add_argument("--since", default=""); p.set_defaults(fn=cmd_sse_logs)
    p = sub.add_parser("inference"); p.add_argument("--url", required=True); p.add_argument("--timeout", type=float, default=5.0)
    p.set_defaults(fn=cmd_inference)
    p = sub.add_parser("triggers"); p.add_argument("--db", required=True); p.set_defaults(fn=cmd_triggers)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
