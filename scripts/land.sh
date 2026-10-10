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
# Merge server-side only. `gh pr merge` also does local git work: it updates the
# base branch and, with --delete-branch, switches to it. Both fail whenever the
# default branch is checked out in another worktree — which is the normal layout
# here, since every task gets its own worktree. The result was a SUCCESSFUL merge
# reported as a non-zero exit, with the remote branch left behind, so callers saw
# a false failure and had to clean up by hand. The API call touches no local state.
merged="$(gh api -X PUT "repos/{owner}/{repo}/pulls/$pr/merge" \
  -f merge_method=squash --jq '.merged')"
if [ "$merged" != "true" ]; then
  echo "!! merge did not report success (merged=$merged)" >&2
  exit 1
fi

branch="$(gh pr view "$pr" --json headRefName --jq .headRefName)"
if [ -n "$branch" ] && [ "$branch" != "main" ] && [ "$branch" != "master" ]; then
  gh api -X DELETE "repos/{owner}/{repo}/git/refs/heads/$branch" >/dev/null 2>&1 \
    || echo "!! remote branch '$branch' not deleted (non-fatal; delete it manually)" >&2
fi
echo "==> landed PR #$pr"
