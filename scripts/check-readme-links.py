#!/usr/bin/env python3
"""CI guard: every relative link / repo path named in README.md resolves on disk.

Stdlib-only (no third-party deps). Resolves each target against the repo root
(the directory that contains README.md) and exits non-zero on the first target
that does not exist.

Two kinds of reference are checked:

1. Markdown inline links — `[label](target)` — always.
2. Inline code spans — `` `target` `` — when the token *looks like* a repo path:
   it contains "/", has no whitespace, has no URL scheme, and either starts with
   a known top-level directory or ends in a file extension. Fenced code blocks
   (```...```) are excluded so ASCII-diagram internals (addresses, image names)
   are not mistaken for paths.

In CI (post-commit) every target is a tracked path, so "exists on disk" is the
same assertion as "resolves to a tracked path".

Usage:
    python3 scripts/check-readme-links.py [path-to-readme]
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_README = REPO / "README.md"

# Top-level directories tracked in the repo, used to recognise path-shaped
# inline code spans. Kept explicit so the heuristic stays conservative.
TOP_DIRS = {
    "chaos", "console", "controller", "dashboard", "deck", "docs",
    "frontend", "image", "infra", "scripts", "termini", "tests", "w7",
    ".github", ".githooks",
}

FILE_EXTS = {
    ".md", ".sh", ".py", ".json", ".yaml", ".yml", ".toml", ".txt",
    ".html", ".css", ".js", ".mjs", ".cjs", ".svg", ".png", ".jpg",
}

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
CODE_RE = re.compile(r"`([^`]+)`")


def strip_fences(text: str) -> str:
    """Remove fenced code blocks (```...```) so diagram internals are not
    mistaken for path-shaped inline code."""
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return "\n".join(out)


def looks_like_path(token: str) -> bool:
    if not token or "/" not in token:
        return False
    if any(c.isspace() for c in token):
        return False
    if token.startswith(("http://", "https://", "mailto:", "<", "#")):
        return False
    if "://" in token:
        return False
    first = token.split("/", 1)[0]
    if first in TOP_DIRS:
        return True
    return os.path.splitext(token)[1].lower() in FILE_EXTS


def resolve_target(target: str) -> str | None:
    """Normalise a link/code target to a repo-root-relative path (anchor off)."""
    t = target.strip().strip("<>")
    if not t:
        return None
    if t.startswith(("http://", "https://", "mailto:")):
        return None
    t = t.split("#", 1)[0].split("?", 1)[0]
    return t or None


def main() -> int:
    readme = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_README
    if not readme.is_absolute():
        readme = REPO / readme
    text = readme.read_text(encoding="utf-8")

    targets: list[tuple[str, str]] = []
    for m in LINK_RE.finditer(text):
        t = resolve_target(m.group(1))
        if t:
            targets.append((t, "link"))

    for m in CODE_RE.finditer(strip_fences(text)):
        token = m.group(1).strip()
        if looks_like_path(token):
            t = resolve_target(token)
            if t:
                targets.append((t, "code"))

    missing: list[tuple[str, str]] = []
    seen: set[str] = set()
    for target, kind in targets:
        if target in seen:
            continue
        seen.add(target)
        path = REPO / target
        if not path.exists():
            missing.append((target, kind))

    print(f"repo root: {REPO}")
    print(f"checked {len(seen)} unique link/path target(s)")
    if missing:
        for target, kind in missing:
            print(f"MISSING ({kind}): {target}", file=sys.stderr)
        print(f"{len(missing)} target(s) do not resolve", file=sys.stderr)
        return 1
    print("all README links and repo paths resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
