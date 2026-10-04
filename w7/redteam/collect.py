#!/usr/bin/env python3
"""collect.py — fold the per-scenario evidence into one machine-readable summary.

    python3 w7/redteam/collect.py <EVIDENCE_ROOT> [OUT_JSON]

<EVIDENCE_ROOT> is the directory `run.sh` wrote: it must contain boot/<scenario>/
and net/<scenario>/ subdirs. Emits a consolidated summary with the headline
numbers the report quotes.
"""
from __future__ import annotations

import json
import pathlib
import sys

BOOT_SCENARIOS = ["baseline", "delay", "refuse", "dnsfail", "badtoken"]
NET_SCENARIOS = ["baseline", "latency", "loss", "kill"]


def load(p: pathlib.Path):
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    root = pathlib.Path(sys.argv[1])
    out = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else root / "summary.json"
    summary: dict = {"boot": {}, "net": {}, "legacy_apt": None}

    for s in BOOT_SCENARIOS:
        d = load(root / "boot" / s / "summary.json")
        if d:
            summary["boot"][s] = d.get("by_scenario", {}).get(s, d)

    for s in NET_SCENARIOS:
        d = load(root / "net" / s / "summary.json")
        if d:
            summary["net"][s] = d

    for cand in ("legacy-apt/legacy-apt-result.txt",):
        p = root / "boot" / cand
        if p.exists():
            summary["legacy_apt"] = p.read_text()

    out.write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
