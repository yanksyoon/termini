#!/usr/bin/env bash
# ==============================================================================
# Termini — record the overlay CIDR + per-node allocations in the shared
# inventory (~/.termini/inventory.json), then validate it against the inventory
# schema. Idempotent: overwrites any previous `overlay` section.
#
# Usage: infra/cluster/record-inventory.sh [--inventory PATH] [--schema PATH] [--key PATH]
# ==============================================================================
set -euo pipefail

INVENTORY="${INVENTORY:-$HOME/.termini/inventory.json}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
SEED="${SEED:-node-a}"
SCHEMA="${SCHEMA:-}"
OV_CIDR="${OV_CIDR:-10.99.0.0/24}"
OV_GW="${OV_GW:-10.99.0.1}"
OV_POOL="${OV_POOL:-10.99.0.100-10.99.0.254}"
NET="${NET:-termini-overlay}"
VNI="${VNI:-100}"
VPORT="${VPORT:-4789}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --inventory) INVENTORY="$2"; shift 2 ;;
    --schema) SCHEMA="$2"; shift 2 ;;
    --key) SSH_KEY="$2"; shift 2 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -f "$INVENTORY" ]] || { echo "inventory not found: $INVENTORY" >&2; exit 1; }
command -v jq >/dev/null || { echo "jq required" >&2; exit 1; }
if [[ -z "$SCHEMA" ]]; then
  SCHEMA="$(cd "$(dirname "$0")/../gcp" && pwd)/inventory.schema.json"
fi

SSH_USER="$(jq -r '.ssh.user // "ubuntu"' "$INVENTORY")"
declare -A PUB
while IFS=$'\t' read -r n pub; do PUB["$n"]="$pub"; done < <(jq -r '.nodes[] | [.name, (.public_ip // "")] | @tsv' "$INVENTORY")

SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null
          -o LogLevel=ERROR -o ConnectTimeout=15 -o BatchMode=yes)

echo "== reading live allocations from $SEED =="
alloc="$(ssh "${SSH_OPTS[@]}" "${SSH_USER}@${PUB[$SEED]}" 'sudo lxc list --format csv -c n,4,L 2>/dev/null' \
  | sed -E 's/ \([^)]*\)//' \
  | awk -F, 'NF>=3 && $2 ~ /^[0-9.]+$/ {printf "{\"instance\":\"%s\",\"node\":\"%s\",\"ipv4\":\"%s\"}\n",$1,$3,$2}' \
  | jq -s '.')"

tmp="$(mktemp)"
jq --argjson alloc "$alloc" \
   --arg cidr "$OV_CIDR" --arg gw "$OV_GW" --arg pool "$OV_POOL" \
   --arg bridge "$NET" --argjson vni "$VNI" --argjson port "$VPORT" '
  .overlay = {
    cidr: $cidr,
    mode: "vxlan-bridge",
    gateway: $gw,
    pool: $pool,
    vni: $vni,
    udp_port: $port,
    bridge: $bridge,
    members: [ .nodes[] | { name: .name, underlay_ip: .private_ip, vtep: .private_ip } ],
    instances: $alloc
  }' "$INVENTORY" > "$tmp"
mv "$tmp" "$INVENTORY"

echo "== wrote overlay section to $INVENTORY =="
jq '.overlay' "$INVENTORY"

echo "== validating against $SCHEMA =="
python3 - "$INVENTORY" "$SCHEMA" <<'PY'
import json
import sys
import jsonschema
schema = json.load(open(sys.argv[2]))
doc = json.load(open(sys.argv[1]))
jsonschema.validate(doc, schema)
print("inventory valid against", sys.argv[2])
PY
