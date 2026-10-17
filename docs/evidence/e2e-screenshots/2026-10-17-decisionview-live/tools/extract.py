#!/usr/bin/env python3
"""Pull the exact API object behind a rendered row out of a raw response body.

Sidecars must carry raw JSON, not a paraphrase, so this prints the matching
object verbatim (json.dumps keeps every value byte-for-byte; it only reindents).

Usage:
  extract.py decision <decisions.json> <model|fallback|deterministic|any>
  extract.py audit    <audits.json> <audit_id>
  extract.py event    <events.json> <event_id>
  extract.py sse      <stream.txt> [max_lines]
"""
import json
import sys


def load(path):
    with open(path) as fh:
        return json.load(fh)


def rows(doc, key):
    if isinstance(doc, dict):
        return doc.get(key) or doc.get("items") or []
    return doc


def main():
    mode = sys.argv[1]
    if mode == "sse":
        lines = [ln for ln in open(sys.argv[2]).read().splitlines() if ln.strip()]
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 40
        print(f"# {len(lines)} non-empty lines in the capture; last {min(n, len(lines))} shown")
        print("\n".join(lines[-n:]))
        return
    doc = load(sys.argv[2])
    want = sys.argv[3]
    if mode == "decision":
        items = rows(doc, "decisions")
        if want != "any":
            items = [d for d in items if d.get("decision_source") == want]
        if not items:
            raise SystemExit(f"no {want} decision in {sys.argv[2]}")
        print(json.dumps(items[0], indent=2, sort_keys=True))
        return
    if mode == "audit":
        items = rows(doc, "audits")
        match = [a for a in items if a.get("id") == want]
        if not match:
            raise SystemExit(f"no audit {want} in {sys.argv[2]}")
        print(json.dumps(match[0], indent=2, sort_keys=True))
        return
    if mode == "event":
        items = rows(doc, "events")
        match = [e for e in items if e.get("id") == want]
        if not match:
            raise SystemExit(f"no event {want} in {sys.argv[2]}")
        print(json.dumps(match[0], indent=2, sort_keys=True))
        return
    raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    main()
