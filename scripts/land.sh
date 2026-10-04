#!/usr/bin/env bash
# Termini land gate — merge the current branch's PR ONLY when CI is green.
#
# Server-side branch protection is unavailable (private repo, free plan), and
# `gh pr merge` will happily merge a PENDING or failing CI, so THIS SCRIPT is
# the enforcement point. Workers MUST land PRs through it — never blind-`gh pr merge`.
#
# Usage: scripts/land.sh [pr-number]     (env: LAND_MAX_WAIT seconds, default 900)
set -euo pipefail

pr="${1:-}"
if [ -z "$pr" ]; then
  pr="$(gh pr view --json number --jq .number)"
fi

MAX_WAIT="${LAND_MAX_WAIT:-900}"
start=$SECONDS
checks=""

echo "==> PR #$pr — waiting for checks to settle (max ${MAX_WAIT}s)"
while :; do
  checks="$(gh pr checks "$pr" 2>&1 || true)"

  if printf '%s\n' "$checks" | grep -qiE '\b(pending|queued|in_progress|waiting)\b'; then
    : # still running — keep polling
  elif printf '%s\n' "$checks" | grep -qiE '\b(pass|fail|skip|neutral|cancel)'; then
    break # settled
  fi
  # else: no checks reported yet — keep polling

  if (( SECONDS - start >= MAX_WAIT )); then
    echo "!! timed out waiting for CI on PR #$pr" >&2
    printf '%s\n' "$checks" >&2
    exit 1
  fi
  sleep 10
done

if printf '%s\n' "$checks" | grep -qiE '\bfail|\bcancel'; then
  echo "!! CI not green on PR #$pr — refusing to merge" >&2
  printf '%s\n' "$checks" >&2
  exit 1
fi

if ! printf '%s\n' "$checks" | grep -qiE '\bpass\b'; then
  echo "!! no passing checks reported on PR #$pr — refusing to merge" >&2
  printf '%s\n' "$checks" >&2
  exit 1
fi

mergeable="$(gh pr view "$pr" --json mergeable --jq .mergeable)"
if [ "$mergeable" != "MERGEABLE" ]; then
  echo "!! PR #$pr is not mergeable (state=$mergeable) — resolve first" >&2
  exit 1
fi

echo "==> CI green — merging PR #$pr"
gh pr merge "$pr" --squash --delete-branch
echo "==> landed PR #$pr"
