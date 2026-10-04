# Termini — agent working agreement

Repo root is **/home/ubuntu** (origin `https://github.com/yanksyoon/termini.git`, branch `main`).
`~/termini/` is just a folder *inside* the repo — NOT the repo root. Task paths are relative to `/home/ubuntu`.

## Delivery flow — every work task lands through a PR

1. **Sync + branch:** `git fetch origin && git checkout -b wt/<task-id>-<slug> origin/main`
2. **Implement** at the exact paths named in the task.
3. **Verify first, iterate until green:** run the acceptance command(s) the task names. Nothing is done unverified.
4. **Commit — stage explicit paths only, never `git add -A`:** `git add <paths…> && git commit -m "<task-id> <type>: <summary>"`
5. **Push:** `git push -u origin HEAD`
6. **Open a PR:** `gh pr create --title "<task-id> <summary>" --body "<what / why / how verified>"`
7. **Land it:** `gh pr checks --watch`; merge once CI is green, or once the reviewer approves when no required check is failing: `gh pr merge --squash --delete-branch`
8. **Record on the card:** metadata `{"commit", "pr", "merge_sha", "files", "verified_with"}`.

## Rules

- Never commit secrets or runtime state (`.hermes/`, `.ssh/`, `.env*`, `*.pem`, `*.key`). A `pre-commit` hook blocks them — do not `--no-verify`.
- On `index.lock`, retry after a moment (shared checkout).
- Keep each PR scoped to one card.

## Reviewers

Check out the PR branch, `git show` the commit, confirm the deliverables, and re-run acceptance against the commit. Request changes with the exact failing command/output, or approve and land.
