#!/usr/bin/env bash
# legacy_apt_repro.sh — reproducible "break-the-demo" for the pre-hardening
# cloud-init path (project.md adversarial note 2).
#
# The frozen decision D3 replaces boot-time installs with an activation-only
# cloud-init. This script demonstrates the failure mode that decision removes:
# a cloud-init that runs `apt-get update && apt-get install` at boot, against a
# mirror that accepts the TCP connection and then never answers (a throttled /
# black-holed mirror). There is no application-level timeout, so cloud-init does
# not finish and the node never registers — a silent hang past the demo budget.
#
#   bash w7/redteam/boot/legacy_apt_repro.sh <OUT_DIR> [BUDGET_S]
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-/tmp/legacy-apt}"
BUDGET="${2:-90}"
NAME=rt-legacy-apt
HOST=10.14.100.1          # lxdbr0 gateway, reachable from the node
PROXY_PORT=8095
mkdir -p "$OUT"

cleanup() {
  [ -n "${PROXY_PID:-}" ] && kill "$PROXY_PID" 2>/dev/null || true
  lxc delete --force "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "### starting stall proxy on :$PROXY_PORT (accepts, never responds)"
FAULT_MODE=stall python3 "$HERE/fault_proxy.py" --listen-port "$PROXY_PORT" --upstream-port 8097 \
  > "$OUT/stall-proxy.log" 2>&1 &
PROXY_PID=$!
sleep 1

cat > "$OUT/user-data.yaml" <<EOF
#cloud-config
# The PRE-HARDENING cloud-init: installs packages at boot (D3 forbids this).
write_files:
  - path: /etc/apt/sources.list.d/legacy-mirror.list
    permissions: '0644'
    content: |
      deb [trusted=yes] http://$HOST:$PROXY_PORT/ubuntu noble main
runcmd:
  - [ sh, -c, "apt-get update && apt-get install -y --no-install-recommends curl" ]
EOF

lxc delete --force "$NAME" >/dev/null 2>&1 || true
echo "### copying fresh node -> $NAME"
lxc copy demo-node-a/unregistered "$NAME" </dev/null >/dev/null 2>&1
lxc config set "$NAME" cloud-init.user-data "$(cat "$OUT/user-data.yaml")"

echo "### start; measuring whether cloud-init completes within ${BUDGET}s"
T0=$(date +%s.%N)
lxc start "$NAME"
DONE=0
for _ in $(seq 1 "$BUDGET"); do
  st=$(lxc exec "$NAME" -- cloud-init status 2>/dev/null | tr -d '\r')
  if printf '%s' "$st" | grep -q 'status: done'; then DONE=1; break; fi
  sleep 1
done
T1=$(date +%s.%N)
ELAPSED=$(awk "BEGIN{printf \"%.1f\", $T1-$T0}")
{
  echo "cloud_init_done_within_budget=$DONE"
  echo "elapsed_s=$ELAPSED"
  echo "budget_s=$BUDGET"
  echo "--- cloud-init status ---"
  lxc exec "$NAME" -- cloud-init status --long 2>&1
  echo "--- apt/provision log tail ---"
  lxc exec "$NAME" -- sh -c 'cat /var/log/cloud-init-output.log 2>/dev/null | tail -20' 2>&1
  echo "--- bootstrap state (expected: never reached) ---"
  lxc exec "$NAME" -- cat /var/lib/termini/state.json 2>&1
} | tee "$OUT/legacy-apt-result.txt"

echo "### result written to $OUT/legacy-apt-result.txt"
