#!/usr/bin/env bash
# fleet-vm.sh — the Termini fleet as LXD VIRTUAL MACHINES on one host.
#
# The production footprint is four GCE instances (controller + node-a + node-c +
# node-d, `infra/gcp/`). This script reproduces the same three-member fleet as
# real KVM guests on a single LXD host — `lxc launch --vm`, not containers — so
# the demo can run with no Compute Engine spend:
#
#   tvm-controller  10.14.100.10   2 vCPU / 2 GiB / 15 GiB   control plane
#   tvm-node-a      10.14.100.20   2 vCPU / 3 GiB / 15 GiB   compute node
#   tvm-node-c      10.14.100.30   2 vCPU / 3 GiB / 15 GiB   inference front door
#
# Why VMs and not containers: a container shares the host kernel, so it cannot
# demonstrate the property the product sells (a real machine that a controller
# drives over its own LXD API). Each guest therefore runs its OWN LXD daemon and
# advertises `https://<guest-ip>:8443` to the controller, exactly as the GCE
# nodes do. That needs nested virtualisation, which this host exposes
# (`/dev/kvm` inside the guest, `kvm_intel.nested=Y`): the fleet's agent workloads
# can themselves be VMs.
#
# Usage:
#   ./fleet-vm.sh create        # create + boot the three VMs (idempotent)
#   ./fleet-vm.sh start|stop    # power the whole fleet
#   ./fleet-vm.sh status        # state, address, guest kernel
#   ./fleet-vm.sh destroy       # delete the VMs (and their disks)
set -euo pipefail

IMAGE="${TERMINI_VM_IMAGE:-u24vm}"          # local alias, see `ensure_image`
REMOTE_IMAGE="${TERMINI_VM_REMOTE_IMAGE:-ubuntu:24.04}"
GATEWAY="${TERMINI_VM_GATEWAY:-10.14.100.1}"

# name:ip:cpu:mem:disk
FLEET=(
  "tvm-controller:10.14.100.10:2:2GiB:15"
  "tvm-node-a:10.14.100.20:2:3GiB:15"
  "tvm-node-c:10.14.100.30:2:3GiB:15"
)

log() { printf '[fleet-vm] %s\n' "$*" >&2; }

ensure_image() {
  if lxc image info "$IMAGE" >/dev/null 2>&1; then return 0; fi
  log "caching $REMOTE_IMAGE as $IMAGE (one-time, ~600 MiB)"
  lxc image copy "$REMOTE_IMAGE" local: --vm --alias "$IMAGE" >/dev/null
}

# The guest NIC is a virtio PCI device, so it comes up as enp5s0, NOT eth0. A
# netplan keyed on eth0 silently leaves the guest with no address at all, so the
# config matches the name and pins it.
netcfg() { # $1 = address
  cat <<EOF
network:
  version: 2
  ethernets:
    eth0:
      match:
        name: "en*"
      set-name: eth0
      dhcp4: false
      addresses: [$1/24]
      routes:
        - to: default
          via: $GATEWAY
      nameservers:
        addresses: [$GATEWAY, 1.1.1.1]
EOF
}

create_one() { # name ip cpu mem disk
  local name=$1 ip=$2 cpu=$3 mem=$4 disk=$5
  if lxc info "$name" >/dev/null 2>&1; then
    log "$name already exists — leaving it"
    return 0
  fi
  local tmp; tmp=$(mktemp)
  netcfg "$ip" > "$tmp"
  log "creating VM $name ($ip, ${cpu} vCPU, $mem, ${disk} GiB)"
  lxc launch "$IMAGE" "$name" --vm \
    --config limits.cpu="$cpu" \
    --config limits.memory="$mem" \
    --config cloud-init.network-config="$(cat "$tmp")" \
    --device root,size="${disk}GiB" >/dev/null
  rm -f "$tmp"
}

cmd_create() {
  ensure_image
  local entry name ip cpu mem disk
  for entry in "${FLEET[@]}"; do
    IFS=: read -r name ip cpu mem disk <<<"$entry"
    create_one "$name" "$ip" "$cpu" "$mem" "$disk"
  done
  log "waiting for guests to answer on their static addresses"
  for entry in "${FLEET[@]}"; do
    IFS=: read -r name ip _ <<<"$entry"
    for _i in $(seq 1 60); do
      if lxc exec "$name" -- sh -c "ip -4 -o addr show eth0 | grep -q $ip" 2>/dev/null; then
        log "$name up at $ip"
        break
      fi
      sleep 3
    done
  done
}

cmd_start()  { for e in "${FLEET[@]}"; do lxc start "${e%%:*}"; done; }
cmd_stop()   { for e in "${FLEET[@]}"; do lxc stop "${e%%:*}"; done; }
cmd_status() {
  printf '%-16s %-9s %-22s %s\n' NAME STATE ADDRESS GUEST
  for e in "${FLEET[@]}"; do
    local name="${e%%:*}"
    local state addr guest
    state=$(lxc list "$name" --format csv -c s 2>/dev/null || echo absent)
    addr=$(lxc list "$name" --format csv -c 4 2>/dev/null | awk '{print $1}')
    guest=$(lxc exec "$name" -- uname -r 2>/dev/null || echo -)
    printf '%-16s %-9s %-22s %s\n' "$name" "$state" "${addr:-—}" "$guest"
  done
}
cmd_destroy() {
  for e in "${FLEET[@]}"; do
    lxc delete --force "${e%%:*}" 2>/dev/null || true
  done
  log "fleet VMs deleted"
}

case "${1:-status}" in
  create)  cmd_create ;;
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  status)  cmd_status ;;
  destroy) cmd_destroy ;;
  *) echo "usage: $0 {create|start|stop|status|destroy}" >&2; exit 2 ;;
esac
