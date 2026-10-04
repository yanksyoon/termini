#!/usr/bin/env bash
# ==============================================================================
# Termini — bring up the single-CIDR overlay across the LXD cluster members.
#
# Builds a VXLAN transit (VNI 100, UDP 4789 — the port the frozen firewall
# already allows) between the members' underlay addresses, bridges it into an
# LXD bridge network `termini-overlay` on every member, and starts ONE
# authoritative DHCP/gateway service on the seed so instances on any member
# obtain an address from the same subnet.
#
# Why VXLAN and not OVN/Fan: see infra/cluster/README.md ("Network mode"). OVN
# needs the OVS/OVN stack plus Geneve/UDP 6081, which the frozen firewall does
# not open; LXD 5.21 rejects `--type=fan` ("Unknown driver").
#
# Usage: infra/cluster/overlay-up.sh [--inventory PATH] [--key PATH] [--seed NAME]
# ==============================================================================
set -euo pipefail

INVENTORY="${INVENTORY:-$HOME/.termini/inventory.json}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
SEED="${SEED:-node-a}"

NET="${NET:-termini-overlay}"
POOL="${POOL:-local}"
OV_CIDR="${OV_CIDR:-10.99.0.0/24}"
OV_GW="${OV_GW:-10.99.0.1}"
VNI="${VNI:-100}"
VPORT="${VPORT:-4789}"
VDEV="${VDEV:-ens4}"
VIF="${VIF:-vxlan-termini}"
BR_MTU="${BR_MTU:-1400}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --inventory) INVENTORY="$2"; shift 2 ;;
    --key) SSH_KEY="$2"; shift 2 ;;
    --seed) SEED="$2"; shift 2 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
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
run_step() { local m="$1" f="$2"; ssh "${SSH_OPTS[@]}" "${SSH_USER}@${PUB[$m]}" \
  "cat > /tmp/termini-step.sh && bash /tmp/termini-step.sh" < "$f"; }

# --- 1. VXLAN transit on every member ----------------------------------------
for m in "${MEMBERS[@]}"; do
  echo "== VXLAN transit on $m (${PRIV[$m]}) =="
  peers=()
  for p in "${MEMBERS[@]}"; do [[ "$p" != "$m" ]] && peers+=("${PRIV[$p]}"); done
  step="$(mktemp)"
  {
    echo "set -euo pipefail"
    echo "if ip link show ${VIF} >/dev/null 2>&1; then echo '  ${VIF} exists'; else"
    echo "  sudo ip link add ${VIF} type vxlan id ${VNI} dev ${VDEV} dstport ${VPORT} local ${PRIV[$m]} nolearning"
    echo "fi"
    echo "sudo ip link set ${VIF} up"
    for p in "${peers[@]}"; do
      echo "sudo bridge fdb append 00:00:00:00:00:00 dev ${VIF} dst ${p}"
      echo "sudo bridge fdb append 01:00:5e:00:00:00 dev ${VIF} dst ${p}"
      echo "sudo bridge fdb append 33:33:00:00:00:00 dev ${VIF} dst ${p}"
    done
    echo "ip -d link show ${VIF} | sed -n '1,2p'"
  } > "$step"
  run_step "$m" "$step"
  rm -f "$step"
done

# --- 2. LXD bridge network over the transit (seed drives the cluster) ---------
echo "== LXD network ${NET} =="
if node_ssh "$SEED" "sudo lxc network show ${NET} >/dev/null 2>&1"; then
  echo "  ${NET} already exists"
else
  for m in "${MEMBERS[@]}"; do
    node_ssh "$SEED" "sudo lxc network create ${NET} --type=bridge --target ${m} bridge.external_interfaces=${VIF}"
  done
  node_ssh "$SEED" "sudo lxc network create ${NET} --type=bridge ipv4.address=none ipv6.address=none bridge.mtu=${BR_MTU}"
fi
node_ssh "$SEED" "sudo lxc network show ${NET}"

# --- 3. default profile: root disk + overlay NIC -----------------------------
echo "== default profile (root disk on ${POOL}, eth0 on ${NET}) =="
node_ssh "$SEED" "sudo lxc profile device add default root disk path=/ pool=${POOL} 2>/dev/null || true"
node_ssh "$SEED" "sudo lxc profile device add default eth0 nic network=${NET} 2>/dev/null || true"
node_ssh "$SEED" "sudo lxc profile show default"

# --- 4. Single authoritative gateway/DHCP on the seed ------------------------
echo "== overlay gateway + DHCP on $SEED (${OV_GW}) =="
step="$(mktemp)"
cat > "$step" <<STEP
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
sudo ip addr replace ${OV_GW}/24 dev ${NET}
if ! command -v dnsmasq >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq dnsmasq
fi
sudo tee /etc/dnsmasq.d/termini-overlay.conf >/dev/null <<'CONF'
interface=${NET}
bind-dynamic
dhcp-range=10.99.0.100,10.99.0.254,255.255.255.0,2h
dhcp-option=3,${OV_GW}
dhcp-option=6,${OV_GW}
dhcp-authoritative
port=0
log-dhcp
CONF
sudo systemctl enable --now dnsmasq
sudo systemctl restart dnsmasq
ip -brief addr show ${NET}
STEP
run_step "$SEED" "$step"
rm -f "$step"

echo "== overlay up: subnet ${OV_CIDR}, gateway/DHCP ${OV_GW}, pool 10.99.0.100-254 =="
