#!/usr/bin/env bash
# Termini land gate — merge the current branch's PR ONLY when CI is green.
#
# Server-side branch protection is unavailable on this private free-plan repo,
# so this script is the enforcement point. Workers MUST land PRs through it —
# never with a blind `gh pr merge`.
#
# Usage: scripts/land.sh [pr-number]
set -euo pipefail

pr="${1:-}"
if [ -z "$pr" ]; then
  pr="$(gh pr view --json number --jq .number)"
fi

echo "==> PR #$pr — waiting for checks"
if ! gh pr checks "$pr" --watch; then
  echo "!! CI is not green — refusing to merge PR #$pr" >&2
  gh pr checks "$pr" >&2 || true
  exit 1
fi

# Belt and braces: re-read and fail on any non-passing check.
if gh pr checks "$pr" 2>/dev/null | grep -qiE '\bfail(ed|ure)?\b|\bpending\b'; then
  echo "!! non-passing checks present — refusing to merge PR #$pr" >&2
  gh pr checks "$pr" >&2 || true
  exit 1
fi

mergeable="$(gh pr view "$pr" --json mergeable --jq .mergeable)"
if [ "$mergeable" != "MERGEABLE" ]; then
  echo "!! PR #$pr is not mergeable ($mergeable) — resolve conflicts first" >&2
  exit 1
fi

echo "==> CI green — merging PR #$pr"
gh pr merge "$pr" --squash --delete-branch
echo "==> landed PR #$pr"
