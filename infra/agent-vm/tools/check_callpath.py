#!/usr/bin/env python3
"""check_callpath.py — host-side acceptance gate for the cross-node call path.

Reads the JSON report the Agent VM produced (via the pulled `/tmp` artifacts),
asserts the card's acceptance, and writes a single machine-readable evidence
document. Exit 0 only when every assertion holds.

Acceptance (t_fd8eee6c): "a multi-turn task from Node A completes with streaming
tokens; latency recorded."

Assertions:
  * the report has exactly `--expect-turns` turns, each completed (has a total_ms);
  * every turn produced >= `--min-chunks-per-turn` discrete streamed deltas
    (incremental SSE arrivals, not one buffered blob);
  * per-turn first-token latency and total latency are recorded (numbers);
  * the Node C reachability probe succeeded (TCP + /health 200) over the direct
    host route;
  * the served model alias is the requested one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--env", required=True)
    ap.add_argument("--probe", default="")
    ap.add_argument("--expect-turns", type=int, required=True)
    ap.add_argument("--min-chunks-per-turn", type=int, default=2)
    ap.add_argument("--node-a", required=True)
    ap.add_argument("--node-c", required=True)
    ap.add_argument("--node-c-ip", required=True)
    ap.add_argument("--inference-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--image-alias", default="")
    ap.add_argument("--image-fingerprint", default="")
    ap.add_argument("--agent-version", default="{}")
    ap.add_argument("--run-log", default="")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    report = _load(args.report)
    env = _load(args.env)
    probe = _load(args.probe) if args.probe else {}
    run_log = ""
    if args.run_log and os.path.isfile(args.run_log):
        with open(args.run_log, encoding="utf-8", errors="replace") as handle:
            run_log = handle.read()

    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    turns = report.get("turns") or []
    check("report_present", "_error" not in report and bool(report),
          report.get("_error", "report parsed"))
    check("turn_count", len(turns) == args.expect_turns,
          f"turns={len(turns)} expected={args.expect_turns}")

    streamed_ok = bool(turns)
    latency_ok = bool(turns)
    for t in turns:
        n = t.get("streamed_deltas", 0)
        if not (t.get("streamed") and n >= args.min_chunks_per_turn):
            streamed_ok = False
        if not isinstance(t.get("first_token_ms"), (int, float)) or \
           not isinstance(t.get("total_ms"), (int, float)):
            latency_ok = False
    check("streaming_per_turn", streamed_ok,
          "every turn streamed >= %d discrete SSE deltas in time" % args.min_chunks_per_turn)
    check("latency_recorded", latency_ok,
          "per-turn first_token_ms + total_ms recorded for every turn")
    check("report_ok_flag", report.get("ok") is True, f"report.ok={report.get('ok')}")
    check("model_alias", report.get("model") == args.model,
          f"model={report.get('model')} expected={args.model}")

    if probe:
        check("node_c_reachable", probe.get("health_status") == 200,
              f"tcp_connect_ms={probe.get('tcp_connect_ms')} health={probe.get('health_status')}")
    else:
        check("node_c_reachable", False, "probe.json missing")

    failed = [c for c in checks if not c["ok"]]
    evidence = {
        "kind": "termini.callpath_evidence",
        "task": "t_fd8eee6c",
        "acceptance": "a multi-turn task from Node A completes with streaming tokens; latency recorded",
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "topology": {
            "node_a": args.node_a,
            "node_c": args.node_c,
            "node_c_ip": args.node_c_ip,
            "inference_url": args.inference_url,
            "route": "direct host route (no OVN overlay); agent VM on Node A SNATs to the node underlay",
        },
        "agent_vm": {
            "image_alias": args.image_alias,
            "image_fingerprint": args.image_fingerprint,
            "env": env,
        },
        "probe": probe,
        "report": report,
        "checks": checks,
        "verdict": "pass" if not failed else "fail",
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(evidence, handle, indent=2)
        handle.write("\n")

    print(f"--- callpath acceptance ({'PASS' if not failed else 'FAIL'}) ---")
    for c in checks:
        print(f"  [{'ok' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}")
    for t in turns:
        print(f"  turn {t.get('turn')}: deltas={t.get('streamed_deltas')} "
              f"(content={t.get('content_deltas')}, reasoning={t.get('reasoning_deltas')}) "
              f"first_token_ms={t.get('first_token_ms')} total_ms={t.get('total_ms')}")
    if run_log:
        print("--- run log tail ---")
        print("\n".join(run_log.strip().splitlines()[-6:]))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
