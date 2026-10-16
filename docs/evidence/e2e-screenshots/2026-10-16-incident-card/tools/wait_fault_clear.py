#!/usr/bin/env python3
"""Wait until no demo-fault injection is still ACTIVE on the controller.

WHY THIS EXISTS
---------------
`run-live-incident.sh` injects one fault per phase. The injector writes its own
telemetry series for the node while an injection is open, so an injection started
while the previous one is still active makes the node's stored samples ALTERNATE
between the two scenarios — and no rule that needs "N consecutive samples" can
ever see N of a kind. (Observed: phase 3's `latency` series interleaved with
phase 2's still-open `oom` series, so `latency-01` never fired and the fallback
row was never produced.) Each phase therefore waits for the previous fault to
clear before it injects.

Usage:
    wait_fault_clear.py --base http://127.0.0.1:PORT --token T --project-id 01... \
        [--timeout 90]
Prints the number of open injections it waited on and the clear state.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def http_get(url: str, token: str, timeout: float = 10.0):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "X-Termini-Version": "1",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode(errors="replace")}


def open_injections(payload: dict) -> list[dict]:
    faults = payload.get("faults")
    if faults is None:
        faults = payload.get("injections") or []
    return [f for f in faults if isinstance(f, dict) and f.get("state") == "active"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--poll", type=float, default=1.0)
    args = ap.parse_args(argv)

    url = f"{args.base.rstrip('/')}/v1/projects/{args.project_id}/demo/faults"
    deadline = time.monotonic() + args.timeout
    waited = 0
    while True:
        status, payload = http_get(url, args.token)
        if status != 200:
            raise SystemExit(f"GET {url} -> HTTP {status} {payload}")
        active = open_injections(payload)
        if not active:
            print(json.dumps({"waited_on": waited, "open": 0, "state": "clear"}))
            return 0
        waited = max(waited, len(active))
        if time.monotonic() >= deadline:
            print(json.dumps({"waited_on": waited, "open": len(active),
                              "state": "still-active-after-timeout",
                              "injection_ids": [a.get("injection_id") for a in active]}))
            return 1
        time.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(main())
