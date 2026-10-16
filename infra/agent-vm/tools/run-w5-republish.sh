#!/usr/bin/env bash
# run-w5-republish.sh — drive the whole t_94303203 acceptance and capture raw evidence.
#
#   1. record the host + node image identity BEFORE (w4 fingerprint, w5 absent);
#   2. place the rebuilt image on nodes A/B (infra/agent-vm/place-agent-image.sh);
#   3. record the node image identity AFTER (w4 retained, w5 present);
#   4. FRESH-launch an instance from agent-os/w5 on each node (no file copying);
#   5. run the three acceptance points (evidence/accept-w5.sh);
#   6. tear the throwaway instances down.
#
# Usage: run-w5-republish.sh [OUTDIR]
set -uo pipefail
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SELF_DIR}/../../.." && pwd)"
OUT="${1:-$SELF_DIR/w5-raw}"
NODES="${TERMINI_NODES:-demo-node-a demo-node-b}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT"

log() { printf '\n=== %s ===\n' "$*"; }
show() { cat "$1"; }

log "0. identity BEFORE ($STAMP)"
{
  echo "--- host: lxc image list agent-vm/2026.10.0 ---"
  lxc image list agent-vm/2026.10.0
  for n in $NODES; do
    echo "--- $n: lxc image list (before) ---"
    lxc exec "$n" -- lxc image list
  done
} > "$OUT/identity-before.txt" 2>&1
show "$OUT/identity-before.txt"

log "1. place (place-agent-image.sh --nodes '$NODES')"
"$REPO_DIR/infra/agent-vm/place-agent-image.sh" --nodes "$NODES" --print-launch-cmd \
  > "$OUT/place.txt" 2>&1
show "$OUT/place.txt"

log "2. identity AFTER"
{
  echo "--- host: lxc image list agent-vm/2026.10.0 ---"
  lxc image list agent-vm/2026.10.0
  lxc image info agent-vm/2026.10.0 | grep -E '^(Fingerprint|Aliases|Created|Architecture):'
  for n in $NODES; do
    echo "--- $n: lxc image list (after) ---"
    lxc exec "$n" -- lxc image list
    echo "--- $n: w5 ---"
    lxc exec "$n" -- lxc image info agent-os/w5 | grep -E '^(Fingerprint|Aliases):'
    echo "--- $n: w4 (kept for rollback) ---"
    lxc exec "$n" -- lxc image info agent-os/w4 | grep -E '^(Fingerprint|Aliases):'
  done
} > "$OUT/identity-after.txt" 2>&1
show "$OUT/identity-after.txt"

for n in $NODES; do
  inst="w5-accept-$(printf '%s' "$n" | tr -c 'a-z0-9' '-')"
  log "3. FRESH launch on $n -> $inst"
  lxc exec "$n" -- lxc delete --force "$inst" >/dev/null 2>&1 || true
  lxc exec "$n" -- lxc launch agent-os/w5 "$inst" -p termini-agent \
    -c cloud-init.user-data="$(lxc exec "$n" -- cat /root/agent-user-data.yaml)" \
    > "$OUT/launch-$n.txt" 2>&1
  echo "exit=$? (0 = launched)" >> "$OUT/launch-$n.txt"
  for _ in $(seq 1 40); do
    st="$(lxc exec "$n" -- lxc exec "$inst" -- systemctl is-active termini-agent 2>/dev/null | tr -d '\r')"
    [ "$st" = "active" ] && break
    sleep 3
  done
  show "$OUT/launch-$n.txt"
  log "4. acceptance on $n/$inst"
  "$SELF_DIR/accept-w5.sh" "$n" "$inst" "$OUT/$n" > "$OUT/accept-$n.txt" 2>&1
  show "$OUT/accept-$n.txt"
done

log "5. raw outputs"
ls -la "$OUT"
