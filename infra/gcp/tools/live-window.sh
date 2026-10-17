#!/usr/bin/env bash
# live-window.sh — take/release THE live-fleet window on the standing controller VM.
#
# The lease itself (infra/gcp/tools/live-window.py) lives on the controller host at
# /var/lib/termini/live-window/live-window.json, so every worker on every
# workstation sees the same owner — and it is still readable when the controller
# process is down (e.g. during a migration pause, which is exactly when a peer is
# most tempted to deploy on top of you).
#
# Usage:
#   infra/gcp/tools/live-window.sh install                  # once per VM (sudo; idempotent)
#   infra/gcp/tools/live-window.sh status
#   infra/gcp/tools/live-window.sh take --owner t_9c1362f6 --ttl 45m --purpose "live acceptance: t_c37aa0dc"
#   infra/gcp/tools/live-window.sh check --owner t_9c1362f6 # 0 owned · 3 held by other · 4 none
#   infra/gcp/tools/live-window.sh release --owner t_9c1362f6
#
# Exit codes are the tool's own (see live-window.py): the refusal path is 3 and it
# names the holder, so a script that ignores the result still prints the truth.
# There is no --force here on purpose.
set -euo pipefail

INSTANCE="${INSTANCE:-termini-controller}"
ZONE="${ZONE:-asia-south1-a}"
STATE_DIR="${TERMINI_LIVE_WINDOW_DIR:-/var/lib/termini/live-window}"
REMOTE_TOOL="${TERMINI_LIVE_WINDOW_BIN:-/usr/local/bin/termini-live-window}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() { sed -n '2,22p' "$0"; }

gssh() { gcloud compute ssh --zone "$ZONE" "$INSTANCE" "$@"; }

if [ "$#" -lt 1 ]; then usage >&2; exit 2; fi
VERB="$1"; shift

case "$VERB" in
  install)
    echo "[live-window] installing $REMOTE_TOOL on $INSTANCE"
    gcloud compute scp --zone "$ZONE" "$HERE/live-window.py" "$INSTANCE:/tmp/termini-live-window.py" >/dev/null
    # shellcheck disable=SC2016  # $ vars are expanded ON THE VM, not here
    gssh --command "sudo -n install -d -m 0755 '$STATE_DIR' && sudo -n install -m 0755 /tmp/termini-live-window.py '$REMOTE_TOOL' && sudo -n rm -f /tmp/termini-live-window.py && sudo -n '$REMOTE_TOOL' --state-dir '$STATE_DIR' status"
    ;;

  take|release|status|check)
    # Quote every passthrough argument for the remote shell, one by one.
    ARGS=()
    for a in "$@"; do ARGS+=("$(printf '%q' "$a")"); done
    # `gcloud compute ssh` does not reliably propagate the remote exit status, so the
    # remote shell ends with a machine-readable marker and we read THAT. Output is held
    # only long enough to strip the marker; stderr (the refusal text) streams through.
    set +e
    OUT="$(gssh --command "sudo -n '$REMOTE_TOOL' --state-dir '$STATE_DIR' $VERB ${ARGS[*]:-}; echo __LW_RC=\$?")"
    transport_rc=$?
    set -e
    if [ "$transport_rc" -ne 0 ] && ! printf '%s' "$OUT" | grep -q '^__LW_RC='; then
      echo "[live-window] could not run the lease tool on $INSTANCE (ssh rc=$transport_rc)" >&2
      echo "[live-window] if it is not installed yet: infra/gcp/tools/live-window.sh install" >&2
      exit 7
    fi
    rc="$(printf '%s\n' "$OUT" | sed -n 's/^__LW_RC=//p' | tail -n1)"
    printf '%s\n' "$OUT" | sed '/^__LW_RC=/d'
    exit "${rc:-7}"
    ;;

  -h|--help) usage ;;
  *) echo "unknown verb: $VERB" >&2; usage >&2; exit 2 ;;
esac
