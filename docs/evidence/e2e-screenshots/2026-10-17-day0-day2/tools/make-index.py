#!/usr/bin/env python3
"""Emit the evidence index rows for a run directory, from the artefacts themselves.

The index must be generated, never hand-written: every sha256 in it is computed
from the file on disk, so a row cannot claim a capture that is not in the pack and
a file cannot be edited without the index disagreeing.

Usage: python3 tools/make-index.py <run-dir>
Prints two markdown tables: raw transcripts, then PNG frames.
"""
from __future__ import annotations

import hashlib
import pathlib
import sys


def sha256(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    run = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    raw = sorted((run / "raw").glob("*.txt"))
    png = sorted((run / "raw").glob("*.png"))

    print("### Raw transcripts (the train of truth)\n")
    print("| # | file | what it is | bytes | lines | sha256 |")
    print("|---|---|---|---|---|---|")
    for p in raw:
        text = p.read_text(errors="replace")
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        what = first[:110].replace("|", "\\|")
        print(f"| {p.stem.split('-')[0]} | `raw/{p.name}` | {what} | {p.stat().st_size} | "
              f"{len(text.splitlines())} | `{sha256(p)}` |")

    if png:
        print("\n### PNG frames\n")
        print("| # | file | bytes | sha256 |")
        print("|---|---|---|---|")
        for p in png:
            print(f"| {p.stem.split('-')[0]} | `raw/{p.name}` | {p.stat().st_size} | `{sha256(p)}` |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
