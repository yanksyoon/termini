#!/usr/bin/env python3
"""Poll GET /v1/operator/decisions until a matching row appears; write the payload.

Used by `run-live-incident.sh` to wait for the operator loop to decide after a
fault is injected, and to keep the RAW API response that the dashboard rendered
as the evidence for that screenshot (no reshaping, no excerpting: the whole
`{"decisions": [...]}` body as the controller returned it).

Usage:
    wait_decision.py --base http://127.0.0.1:PORT --token T --out row.json \
        [--match incident_generated=model] [--null incident_generated] \
        [--timeout 120]
Prints {"found": true, "decisions": N, "row": {...}} (the matched row verbatim).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import time
import urllib.error
import urllib.request


def get(url: str, token: str, timeout: float = 15.0):
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


def matches(row: dict, equal: dict, nulls: list) -> bool:
    for key, want in equal.items():
        if str(row.get(key)) != want:
            return False
    for key in nulls:
        if row.get(key) is not None:
            return False
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--poll", type=float, default=1.0)
    ap.add_argument("--match", action="append", default=[])
    ap.add_argument("--null", action="append", default=[])
    args = ap.parse_args(argv)

    equal = {}
    for spec in args.match:
        key, _, want = spec.partition("=")
        equal[key] = want

    url = f"{args.base.rstrip('/')}/v1/operator/decisions?limit={args.limit}"
    deadline = time.monotonic() + args.timeout
    payload: dict = {}
    row = None
    while True:
        status, payload = get(url, args.token)
        if status != 200:
            raise SystemExit(f"GET {url} -> HTTP {status} {payload}")
        rows = payload.get("decisions") or []
        for candidate in rows:
            if matches(candidate, equal, args.null):
                row = candidate
                break
        if row is not None:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(args.poll)

    pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.out).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "found": row is not None,
        "decisions": len(payload.get("decisions") or []),
        "row": row,
    }, indent=2, sort_keys=True))
    return 0 if row is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
