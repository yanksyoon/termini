#!/usr/bin/env bash
# ==============================================================================
# Termini — form the 3-member LXD cluster across Nodes A/B/C (infra-decisions.md
# D1: one LXD cluster, pinned snap, local ZFS pool per member — D7).
#
# Bootstraps node-a as the cluster seed and joins node-b/node-c with single-use
# join tokens minted by `lxc cluster add`. Reads the shared inventory written by
# infra/gcp. Re-running is safe: members already ONLINE are left untouched.
#
# Usage: infra/cluster/cluster-up.sh [--inventory PATH] [--key PATH] [--seed NAME]
#   INVENTORY  shared inventory JSON         (default ~/.termini/inventory.json)
#   --key      SSH private key               (default ~/.ssh/id_ed25519)
#   --seed     cluster seed member name      (default node-a)
# ==============================================================================
set -euo pipefail

INVENTORY="${INVENTORY:-$HOME/.termini/inventory.json}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
SEED="${SEED:-node-a}"
POOL="${POOL:-local}"
POOL_SIZE="${POOL_SIZE:-8GiB}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --inventory) INVENTORY="$2"; shift 2 ;;
    --key) SSH_KEY="$2"; shift 2 ;;
    --seed) SEED="$2"; shift 2 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -f "$INVENTORY" ]] || { echo "inventory not found: $INVENTORY" >&2; exit 1; }
command -v jq >/dev/null || { echo "jq required" >&2; exit 1; }

SSH_USER="$(jq -r '.ssh.user // "ubuntu"' "$INVENTORY")"
declare -A PUB PRIV
while IFS=$'\t' read -r n pub priv; do PUB["$n"]="$pub"; PRIV["$n"]="$priv"; done \
  < <(jq -r '.nodes[] | [.name, (.public_ip // ""), .private_ip] | @tsv' "$INVENTORY")
MEMBERS=()
while IFS= read -r n; do MEMBERS+=("$n"); done < <(jq -r '.nodes[].name' "$INVENTORY")

SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null
          -o LogLevel=ERROR -o ConnectTimeout=15 -o BatchMode=yes)

node_ssh() { local m="$1"; shift; ssh "${SSH_OPTS[@]}" "${SSH_USER}@${PUB[$m]}" "$@"; }
member_online() { node_ssh "$SEED" 'sudo lxc cluster list --format csv 2>/dev/null' | grep -E "^$1," | grep -q ONLINE; }

echo "== cluster members: ${MEMBERS[*]} (seed: $SEED) =="

# --- Bootstrap the seed -------------------------------------------------------
if member_online "$SEED"; then
  echo "== seed $SEED is already in a cluster — skipping bootstrap"
else
  echo "== bootstrapping seed $SEED (${PRIV[$SEED]}) =="
  tmp="$(mktemp)"
  cat > "$tmp" <<YAML
config:
  core.https_address: ${PRIV[$SEED]}:8443
storage_pools:
- name: ${POOL}
  driver: zfs
  config:
    size: ${POOL_SIZE}
cluster:
  server_name: ${SEED}
  enabled: true
  member_config: []
YAML
  ssh "${SSH_OPTS[@]}" "${SSH_USER}@${PUB[$SEED]}" "sudo bash -c 'lxd init --preseed'" < "$tmp"
  rm -f "$tmp"
fi

# --- Join the remaining members ----------------------------------------------
for m in "${MEMBERS[@]}"; do
  [[ "$m" == "$SEED" ]] && continue
  if member_online "$m"; then
    echo "== member $m already ONLINE — skipping join"
    continue
  fi
  echo "== joining member $m (${PRIV[$m]}) =="
  tok="$(node_ssh "$SEED" "sudo lxc cluster add $m 2>/dev/null" | grep -oE 'eyJ[A-Za-z0-9+/=]+' | head -1)"
  [[ -n "$tok" ]] || { echo "failed to mint a join token for $m" >&2; exit 1; }
  tmp="$(mktemp)"
  cat > "$tmp" <<YAML
cluster:
  enabled: true
  server_address: ${PRIV[$m]}:8443
  cluster_token: ${tok}
  member_config:
  - entity: storage-pool
    name: ${POOL}
    key: source
    value: ""
YAML
  ssh "${SSH_OPTS[@]}" "${SSH_USER}@${PUB[$m]}" "sudo bash -c 'lxd init --preseed'" < "$tmp"
  rm -f "$tmp"
done

echo "== cluster state =="
node_ssh "$SEED" 'sudo lxc cluster list'
