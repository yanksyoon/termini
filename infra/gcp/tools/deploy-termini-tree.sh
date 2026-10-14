#!/usr/bin/env bash
# deploy-termini-tree.sh — deploy the repo's runtime tree to /opt/termini on the
# controller VM, from a committed ref, with a backup and a verifiable result.
#
# Why this exists: the controller runs from /opt/termini, which is a plain COPY —
# not a git checkout — so nothing linked the running code to a commit. A hand-copy
# of "the changed files" left the deployment silently behind main, and there was no
# way to say WHICH commits the running controller had. This script makes the
# deployment reproducible and self-describing: it archives an exact ref and writes
# `DEPLOYED_COMMIT` (the sha) plus `DEPLOYED_FILES` (a sha256 manifest) into
# /opt/termini, so "is the deployment current?" is a one-command diff against a
# fresh archive.
#
# What it deploys: the directories the controller + seeder actually import/run
# (`controller/ termini/ console/ chaos/`). Local-only files that are NOT in the
# repo (e.g. the demo's lxd_stub.py / inference-proxy.py) are left untouched —
# `tar -x` merges, it does not wipe the target.
#
# It does NOT touch the systemd units or their drop-ins (they are root-owned and
# carry the deployment-specific env), and it never writes to the database.
#
# Usage:
#   infra/gcp/tools/deploy-termini-tree.sh                      # origin/main
#   infra/gcp/tools/deploy-termini-tree.sh --ref <sha|branch>
#   infra/gcp/tools/deploy-termini-tree.sh --instance termini-controller --zone asia-south1-a
set -euo pipefail

REF="${REF:-origin/main}"
INSTANCE="${INSTANCE:-termini-controller}"
ZONE="${ZONE:-asia-south1-a}"
TARGET="${TARGET:-/opt/termini}"
PATHS=(controller termini console chaos)
DO_RESTART=1

while [ "$#" -gt 0 ]; do
  case "$1" in
    --ref) REF="$2"; shift ;;
    --instance) INSTANCE="$2"; shift ;;
    --zone) ZONE="$2"; shift ;;
    --target) TARGET="$2"; shift ;;
    --no-restart) DO_RESTART=0 ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

COMMIT="$(git rev-parse "$REF")"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARBALL="$(mktemp -t termini-tree-XXXXXX.tar.gz)"
trap 'rm -f "$TARBALL"' EXIT

git archive --format=tar.gz -o "$TARBALL" "$COMMIT" "${PATHS[@]}"
echo "[deploy] ref=$REF commit=$COMMIT tarball=$(wc -c <"$TARBALL") bytes"

REMOTE_SCRIPT="$(mktemp -t termini-deploy-XXXXXX.sh)"
cat >"$REMOTE_SCRIPT" <<REMOTE
set -euo pipefail
TARGET="$TARGET"
COMMIT="$COMMIT"
STAMP="$STAMP"
umask 022
echo "[remote] backup -> \${TARGET}.bak-\${STAMP}"
sudo tar -czf "\${TARGET}.bak-\${STAMP}.tgz" -C "\$(dirname "\$TARGET")" "\$(basename "\$TARGET")"
tar -xzf /tmp/termini-tree.tgz -C "\$TARGET"
echo "\$COMMIT" > "\$TARGET/DEPLOYED_COMMIT"
( cd "\$TARGET" && find controller termini console chaos -type f ! -path '*__pycache__*' \\
    | sort | xargs sha256sum ) > "\$TARGET/DEPLOYED_FILES"
echo "[remote] deployed \$(wc -l < "\$TARGET/DEPLOYED_FILES") files from \$COMMIT"
REMOTE

gcloud compute scp --zone "$ZONE" "$TARBALL" "$INSTANCE:/tmp/termini-tree.tgz" >/dev/null
gcloud compute ssh --zone "$ZONE" "$INSTANCE" --command "bash -s" <"$REMOTE_SCRIPT"

if [ "$DO_RESTART" = "1" ]; then
  echo "[deploy] restarting termini-controller"
  gcloud compute ssh --zone "$ZONE" "$INSTANCE" \
    --command 'sudo systemctl restart termini-controller && sleep 6 && systemctl is-active termini-controller'
fi

echo "[deploy] done: $INSTANCE $TARGET now carries $COMMIT"
