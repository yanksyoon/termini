#!/usr/bin/env bash
# place-agent-image.sh — pre-distribute the baked agent VM image to Termini nodes.
#
# Card: t_7c40f9a3 ("Template Agent VM as LXD image and place across Nodes A/B").
# Alias: t_94303203 republished the image as `agent-os/w5` (the §5.6 task surface +
# the `gemma` alias the node serves). `agent-os/w4` is deliberately NOT deleted on
# the nodes, so a switch back is `--node-alias agent-os/w4`.
#
# For each node this script:
#   1. (idempotently) ensures the node's stock LXD is initialised: a `local`
#      storage pool and the `lxdbr0` guest bridge (10.20.<n>.0/24, D2).
#   2. imports the baked image tarball into the node's *local* image store under
#      the alias `agent-os/w5`. Nothing is pulled from a registry/remote: the
#      tarball is copied in and imported, so the alias resolves locally.
#   3. creates the `termini-agent` LXD profile and renders the launch-time
#      cloud-init user-data (project SSH key) to /root/agent-user-data.yaml.
#
# Idempotent and safe to re-run after a node is rebuilt. The documented launch
# command then needs no network at all:
#
#   lxc launch agent-os/w5 agent-test -p termini-agent \
#     -c cloud-init.user-data="$(cat /root/agent-user-data.yaml)"
#
# Usage:
#   place-agent-image.sh [options]
#     --nodes "N1 N2"        nodes to place onto (default: demo-node-a demo-node-b)
#     --host-alias ALIAS     source image alias on this host (default agent-vm/2026.10.0)
#     --node-alias ALIAS     alias to create on each node (default agent-os/w5)
#     --profile NAME         profile to create (default termini-agent)
#     --pool NAME            storage pool name to ensure (default local)
#     --tarball PATH         use a prebuilt image tarball instead of exporting
#     --project-ssh-key FILE project PUBLIC key to inject (default $TERMINI_PROJECT_SSH_PUBKEY
#                            or ~/.ssh/termini_breakglass.pub)
#     --no-ensure-lxd        do not initialise LXD on the node (must already be set up)
#     --print-launch-cmd     after placing, print the launch command per node
#     -h|--help
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST_ALIAS="${TERMINI_HOST_IMAGE_ALIAS:-agent-vm/2026.10.0}"
NODE_ALIAS="${TERMINI_NODE_IMAGE_ALIAS:-agent-os/w5}"
PROFILE="${TERMINI_AGENT_PROFILE:-termini-agent}"
POOL="${TERMINI_POOL:-local}"
NODES_STR="${TERMINI_NODES:-demo-node-a demo-node-b}"
PROJECT_KEY="${TERMINI_PROJECT_SSH_PUBKEY:-$HOME/.ssh/termini_breakglass.pub}"
TARBALL=""
ENSURE_LXD=1
PRINT_CMD=0
USER_DATA_TEMPLATE="$SELF_DIR/agent-user-data.cloud-init.yaml"

log() { printf '[place] %s\n' "$*" >&2; }
die() { printf '[place] FATAL: %s\n' "$*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --nodes) NODES_STR="$2"; shift 2 ;;
    --host-alias) HOST_ALIAS="$2"; shift 2 ;;
    --node-alias) NODE_ALIAS="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --pool) POOL="$2"; shift 2 ;;
    --tarball) TARBALL="$2"; shift 2 ;;
    --project-ssh-key) PROJECT_KEY="$2"; shift 2 ;;
    --no-ensure-lxd) ENSURE_LXD=0; shift ;;
    --print-launch-cmd) PRINT_CMD=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -r "$PROJECT_KEY" ] || die "project SSH public key not readable: $PROJECT_KEY"
[ -r "$USER_DATA_TEMPLATE" ] || die "missing cloud-init template: $USER_DATA_TEMPLATE"

# Guest subnet per node, per infra-decisions.md D2 (10.20.1/2/3.0/24 for A/B/C).
subnet_for() {
  case "$1" in
    *a|*a-*) echo "10.20.1.1/24" ;;   # Node A
    *b|*b-*) echo "10.20.2.1/24" ;;   # Node B
    *c|*c-*) echo "10.20.3.1/24" ;;   # Node C
    *)       echo "10.20.9.1/24" ;;
  esac
}

remote() { local n="$1"; shift; lxc exec "$n" -- "$@"; }

host_fp() { lxc image info "$HOST_ALIAS" 2>/dev/null | awk '/^Fingerprint:/{print $2}'; }
node_fp() { remote "$1" lxc image info "$NODE_ALIAS" 2>/dev/null | awk '/^Fingerprint:/{print $2}'; }

ensure_node_lxd() { # NODE
  local n="$1" subnet; subnet="$(subnet_for "$n")"
  if ! remote "$n" lxc storage show "$POOL" >/dev/null 2>&1; then
    log "$n: creating storage pool $POOL (dir)"
    remote "$n" lxc storage create "$POOL" dir
  fi
  if ! remote "$n" lxc network show lxdbr0 >/dev/null 2>&1; then
    log "$n: creating network lxdbr0 ($subnet, nat)"
    remote "$n" lxc network create lxdbr0 "ipv4.address=$subnet" ipv4.nat=true ipv6.address=none
  fi
  # Node default profile gets a root disk + one NIC so bare `lxc launch` works too.
  if ! remote "$n" lxc profile device show default 2>/dev/null | grep -q '^root:'; then
    remote "$n" lxc profile device add default root disk "path=/" "pool=$POOL"
  fi
  if ! remote "$n" lxc profile device show default 2>/dev/null | grep -q '^eth0:'; then
    remote "$n" lxc profile device add default eth0 nic name=eth0 network=lxdbr0
  fi
}

ensure_profile() { # NODE
  local n="$1"
  remote "$n" lxc profile show "$PROFILE" >/dev/null 2>&1 || remote "$n" lxc profile create "$PROFILE"
  remote "$n" lxc profile set "$PROFILE" limits.cpu 1
  remote "$n" lxc profile set "$PROFILE" limits.memory 512MiB
  remote "$n" lxc profile device show "$PROFILE" | grep -q '^root:' \
    || remote "$n" lxc profile device add "$PROFILE" root disk "path=/" "pool=$POOL"
  remote "$n" lxc profile device show "$PROFILE" | grep -q '^eth0:' \
    || remote "$n" lxc profile device add "$PROFILE" eth0 nic name=eth0 network=lxdbr0
}

render_user_data() { # NODE
  local n="$1" key tmp
  key="$(cat "$PROJECT_KEY")"
  tmp="$(mktemp)"
  sed "s|@@PROJECT_SSH_KEY@@|$key|" "$USER_DATA_TEMPLATE" > "$tmp"
  lxc file push "$tmp" "$n/root/agent-user-data.yaml" >/dev/null
  rm -f "$tmp"
}

# Place the image tarball on one node: export (host) -> push -> import (local store).
place_image() { # NODE
  local n="$1"
  if [ ! -s "$TARBALL" ]; then
    log "$n: exporting $HOST_ALIAS from this host -> $TARBALL"
    [ -n "$WORK" ] || die "--tarball was not provided and no work dir exists"
    lxc image export "$HOST_ALIAS" "${TARBALL%.tar.gz}" >/dev/null 2>&1 \
      || die "lxc image export $HOST_ALIAS failed"
  fi
  log "$n: pushing $(du -h "$TARBALL" | cut -f1) image tarball"
  lxc file push "$TARBALL" "$n/root/agent-vm-image.tar.gz" >/dev/null
  log "$n: importing into local image store as $NODE_ALIAS"
  remote "$n" lxc image import /root/agent-vm-image.tar.gz --alias "$NODE_ALIAS"
  remote "$n" rm -f /root/agent-vm-image.tar.gz
}

WORK=""
cleanup() { [ -n "$WORK" ] && rm -rf "$WORK"; }
trap cleanup EXIT

if [ -z "$TARBALL" ]; then
  WORK="$(mktemp -d)"
  TARBALL="$WORK/agent-vm-image.tar.gz"
fi

WANT_FP="$(host_fp)"
log "host alias $HOST_ALIAS fingerprint=${WANT_FP:-?}"

for n in $NODES_STR; do
  log "=== node $n ==="
  lxc info "$n" >/dev/null 2>&1 || die "node $n not found in this LXD"

  [ "$ENSURE_LXD" = 1 ] && ensure_node_lxd "$n"

  cur_fp="$(node_fp "$n" || true)"
  if [ -n "$WANT_FP" ] && [ "$cur_fp" = "$WANT_FP" ]; then
    log "$n: alias $NODE_ALIAS already present (fingerprint $cur_fp) — skipping import"
  elif [ -n "$cur_fp" ]; then
    log "$n: replacing stale alias $NODE_ALIAS ($cur_fp != $WANT_FP)"
    remote "$n" lxc image delete "$NODE_ALIAS"
    place_image "$n"
  else
    place_image "$n"
  fi

  ensure_profile "$n"
  render_user_data "$n"
  log "$n: placed — $(remote "$n" lxc image list "$NODE_ALIAS" --format csv -c lf 2>/dev/null | head -1)"

  if [ "$PRINT_CMD" = 1 ]; then
    printf 'lxc exec %s -- lxc launch %s agent-test -p %s -c cloud-init.user-data="$(cat /root/agent-user-data.yaml)"\n' \
      "$n" "$NODE_ALIAS" "$PROFILE"
  fi
done

log "done: $NODES_STR"
