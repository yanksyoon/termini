#!/usr/bin/env bash
# 60-node-bootstrap.sh — bake the canonical node bootstrap runtime into the image.
#
# Installs the SAME two artefacts the node-side bootstrap flow ships (and that the
# warm layer symlinks to), so `agentic-os/w3` carries no drift and no node needs a
# provision-time push (infra/bootstrap/provision-node.sh REFRESH_RUNTIME):
#
#   infra/bootstrap/node/termini-bootstrap.py
#        -> /usr/local/bin/termini-bootstrap             (0755)
#   infra/bootstrap/node/termini-bootstrap.service
#        -> /etc/systemd/system/termini-bootstrap.service (0644, enabled)
#
# The unit keeps the first-boot offline preflight ordered before it (the unit's
# own After=termini-firstboot-preflight.service); the preflight itself is baked by
# the pipeline (packer file provisioner / first-boot-preflight.sh) and is enabled
# here too when present.
#
# Source resolution (first match wins) so the step is usable from Packer (staged
# /tmp copies), distrobuilder, and the local warm re-bake:
#   $TERMINI_BOOTSTRAP_PY / $TERMINI_BOOTSTRAP_UNIT  explicit overrides
#   /tmp/termini-bootstrap.py / /tmp/termini-bootstrap.service   staged copies
#   <repo>/infra/bootstrap/node/...                  in-tree fallback
#
# Idempotent. DESTDIR is honoured for an off-image rehearsal (see
# tools/rehearse_bake.py); with DESTDIR set the systemctl enable step is skipped.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
DESTDIR="${DESTDIR:-}"
DESTDIR="${DESTDIR%/}"

resolve() { # explicit - staged - in-tree-relative
  if [ -n "$1" ]; then printf '%s' "$1"; return 0; fi
  if [ -f "$2" ]; then printf '%s' "$2"; return 0; fi
  printf '%s' "$REPO/$3"
}

PY="$(resolve "${TERMINI_BOOTSTRAP_PY:-}" /tmp/termini-bootstrap.py \
        infra/bootstrap/node/termini-bootstrap.py)"
UNIT="$(resolve "${TERMINI_BOOTSTRAP_UNIT:-}" /tmp/termini-bootstrap.service \
        infra/bootstrap/node/termini-bootstrap.service)"

for f in "$PY" "$UNIT"; do
  [ -f "$f" ] || { echo "FATAL: bootstrap source not found: $f" >&2; exit 1; }
done

install -D -m 0755 "$PY"   "$DESTDIR/usr/local/bin/termini-bootstrap"
install -D -m 0644 "$UNIT" "$DESTDIR/etc/systemd/system/termini-bootstrap.service"

# Keep the unit enabled. (Skipped for the DESTDIR rehearsal, where there is no
# systemd; both enables are best-effort so a bake that ships only the daemon/unit
# — e.g. without the preflight — still succeeds.)
if [ -z "$DESTDIR" ] && command -v systemctl >/dev/null 2>&1; then
  systemctl enable termini-bootstrap.service 2>/dev/null || true
  systemctl enable termini-firstboot-preflight.service 2>/dev/null || true
  systemctl daemon-reload 2>/dev/null || true
fi

bytes="$(stat -c %s "$DESTDIR/usr/local/bin/termini-bootstrap")"
if [ -n "$DESTDIR" ]; then
  echo "[provision:60-node-bootstrap] baked termini-bootstrap (${bytes} bytes) + unit (DESTDIR rehearsal; enable skipped)"
else
  echo "[provision:60-node-bootstrap] baked termini-bootstrap (${bytes} bytes) + termini-bootstrap.service (enabled)"
fi
