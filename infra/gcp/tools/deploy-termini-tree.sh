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
# (`controller/ termini/ console/ chaos/` plus `infra/bootstrap/`, which holds the
# client-facing install artifacts the controller serves at /install/ —
# docs/contracts/controller-api.md §4.7). Local-only files that are NOT in the
# repo (e.g. the demo's lxd_stub.py / inference-proxy.py) are left untouched —
# `tar -x` merges, it does not wipe the target.
#
# It does NOT touch the systemd units or their drop-ins (they are root-owned and
# carry the deployment-specific env), and it never writes to the database.
#
# LIVE WINDOW (kanban t_9c1362f6): a deploy replaces the tree and restarts the
# controller, and `expire_stale_on_boot` then expires every injection another
# worker's process was driving. So a deploy must be inside a CLAIMED live window:
# before touching the target it asks the lease tool on the VM
# (/usr/local/bin/termini-live-window, installed by tools/live-window.sh install)
# whether --actor holds one. Held by someone else → exit 3, nothing changes. Not
# held at all → exit 4, nothing changes (`--no-window` overrides, loudly). It also
# writes `DEPLOY_ACTOR` (actor, ref, commit, time) next to DEPLOYED_COMMIT so a
# controller restart can name who caused it instead of blaming "the operator".
#
# Usage:
#   infra/gcp/tools/deploy-termini-tree.sh --actor t_9c1362f6     # origin/main, inside your window
#   infra/gcp/tools/deploy-termini-tree.sh --ref <sha|branch> --actor t_9c1362f6
#   infra/gcp/tools/deploy-termini-tree.sh --instance termini-controller --zone asia-south1-a
#   infra/gcp/tools/deploy-termini-tree.sh --actor bootstrap --no-window   # repo bootstrap only
set -euo pipefail

REF="${REF:-origin/main}"
INSTANCE="${INSTANCE:-termini-controller}"
ZONE="${ZONE:-asia-south1-a}"
TARGET="${TARGET:-/opt/termini}"
ACTOR="${TERMINI_ACTOR:-}"
WINDOW_MODE="check"
WINDOW_STATE_DIR="${TERMINI_LIVE_WINDOW_DIR:-/var/lib/termini/live-window}"
WINDOW_TOOL="/usr/local/bin/termini-live-window"
PATHS=(controller termini console chaos infra/bootstrap)
DO_RESTART=1

while [ "$#" -gt 0 ]; do
  case "$1" in
    --ref) REF="$2"; shift ;;
    --instance) INSTANCE="$2"; shift ;;
    --zone) ZONE="$2"; shift ;;
    --target) TARGET="$2"; shift ;;
    --actor) ACTOR="$2"; shift ;;
    --no-window) WINDOW_MODE="skip" ;;
    --no-restart) DO_RESTART=0 ;;
    -h|--help) sed -n '2,44p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ -z "$ACTOR" ]; then
  ACTOR="unattributed"
  echo "[deploy] WARNING: no --actor given — the deployment will be recorded as" >&2
  echo "[deploy]          'unattributed', and a controller restart it causes cannot" >&2
  echo "[deploy]          name a card. Pass --actor t_xxxxxxxx next time." >&2
fi

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
ACTOR="$ACTOR"
REF="$REF"
WINDOW_MODE="$WINDOW_MODE"
WINDOW_STATE_DIR="$WINDOW_STATE_DIR"
WINDOW_TOOL="$WINDOW_TOOL"
umask 022

# ---- live window gate (kanban t_9c1362f6) -------------------------------------
if [ "\$WINDOW_MODE" != "skip" ]; then
  if sudo -n test -x "\$WINDOW_TOOL"; then
    set +e
    sudo -n "\$WINDOW_TOOL" --state-dir "\$WINDOW_STATE_DIR" check --owner "\$ACTOR"
    WRC=\$?
    set -e
    case "\$WRC" in
      0) echo "[remote] live window: held by \$ACTOR — deploying inside it" ;;
      3) echo "[remote] ABORTED (exit 3): another owner holds the live window." >&2
         echo "[remote]   Nothing was written to \$TARGET. Wait for the release, or ask" >&2
         echo "[remote]   the holder on the board (infra/gcp/tools/live-window.sh status)." >&2
         exit 3 ;;
      4) echo "[remote] ABORTED (exit 4): no live window is claimed for \$ACTOR." >&2
         echo "[remote]   Take one first:" >&2
         echo "[remote]     infra/gcp/tools/live-window.sh take --owner \$ACTOR --ttl 45m --purpose '...'" >&2
         echo "[remote]   (--no-window exists for repo bootstrap only.)" >&2
         exit 4 ;;
      *) echo "[remote] ABORTED (exit 6): live-window check failed rc=\$WRC" >&2; exit 6 ;;
    esac
  else
    echo "[remote] WARNING: \$WINDOW_TOOL is not installed — deploying WITHOUT the" >&2
    echo "[remote]          live-window check. Install it: infra/gcp/tools/live-window.sh install" >&2
  fi
fi

echo "[remote] backup -> \${TARGET}.bak-\${STAMP}"
sudo tar -czf "\${TARGET}.bak-\${STAMP}.tgz" -C "\$(dirname "\$TARGET")" "\$(basename "\$TARGET")"
tar -xzf /tmp/termini-tree.tgz -C "\$TARGET"
echo "\$COMMIT" > "\$TARGET/DEPLOYED_COMMIT"
# Who deployed this tree: read by the controller at boot, so a restart that expires
# a peer's open injection is attributed to a card instead of "the operator".
printf '{"schema_version":"1.0","actor":"%s","ref":"%s","commit":"%s","deployed_at":"%s","window":"%s"}\n' \\
  "\$ACTOR" "\$REF" "\$COMMIT" "\$STAMP" "\$WINDOW_MODE" > "\$TARGET/DEPLOY_ACTOR"
echo "[remote] deployed by \$ACTOR (window: \$WINDOW_MODE)"
( cd "\$TARGET" && find controller termini console chaos infra/bootstrap -type f ! -path '*__pycache__*' \\
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
