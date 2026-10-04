#!/usr/bin/env python3
"""CI guard: every example identifier in the spec docs is a valid 26-char ULID.

The frozen ID scheme (controller-api.md §1.3, governance.md §4,
vm-allocation.schema.json $defs.ulid, controller/app/ulid.py) is a 26-char
Crockford base32 string (uppercase, alphabet 0-9A-HJKMNP-TV-Z, no I/L/O/U).

This script scans the spec docs and fails if it finds any Crockford string of
length 26..34 that is NOT exactly 26 chars (e.g. the 30-char example IDs that
previously shipped and failed schema self-validation — W1 review D4). The review
record `docs/contracts/w1-review.md` quotes the pre-fix values and is exempt.

Exit 0 on success, 1 on any violation.
"""
from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
CROCKFORD_26PLUS = re.compile(r"\b[0-9A-HJKMNP-TV-Z]{26,34}\b")
EXEMPT = {"docs/contracts/w1-review.md"}

CROCKFORD = frozenset("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def main() -> int:
    violations: list[str] = []
    for fp in sorted(DOCS.rglob("*")):
        if fp.suffix not in (".md", ".json"):
            continue
        rel = str(fp.relative_to(REPO))
        if rel in EXEMPT:
            continue
        for lineno, line in enumerate(fp.read_text().splitlines(), 1):
            for match in CROCKFORD_26PLUS.findall(line):
                if len(match) != 26:
                    violations.append(f"{rel}:{lineno}: {len(match)}-char identifier {match}")
    if violations:
        print("Non-26-char Crockford identifiers in spec docs:")
        for v in violations:
            print("  " + v)
        return 1
    print("all example identifiers are 26-char ULIDs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
