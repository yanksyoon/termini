#!/usr/bin/env bash
# prewarm-images.sh — make the pre-baked OS images resident on every demo node's
# disk, so a node can boot and launch an agent VM with ZERO network fetches.
#
# Card: t_f0017269 ("Pre-warm pre-baked OS images on nodes A/B/C").
#
# Two pre-baked images matter to a node:
#   * the HOST node image  (alias `agentic-os/w3`)  — the OS the node itself booted;
#   * the AGENT VM image   (alias `agent-vm/2026.10.0`) — the workload image the
#     node launches agent VMs from, per-node alias `agent-os/w4`.
#
# For each node this script:
#   1. (idempotently) ensures the node's stock LXD is set up: a `local` dir
#      storage pool, the `lxdbr0` guest bridge (10.20.<n>.0/24), and a `default`
#      profile carrying one root disk + one NIC;
#   2. caches BOTH image tarballs on the node's disk under
#      /var/lib/termini/images/, each with a sha256 sidecar — the on-disk
#      artifacts D3 says the workload image is imported from;
#   3. imports the AGENT image INTO the node's local LXD image store from that
#      cached tarball under the pinned alias, so `lxc launch agent-os/w4` resolves
#      on-box with no registry/remote pull.
#
# Idempotent: a re-run is a no-op once the node alias fingerprint and every
# cached tarball digest already match. Nothing here pulls from a registry: the
# host exports once, then every node receives a copy on its own disk.
#
# Usage:
#   prewarm-images.sh [options]
#     --nodes "N1 N2 N3"    nodes to pre-warm (default demo-node-a demo-node-b demo-node-c)
#     --host-alias ALIAS    host node image alias   (default agentic-os/w3)
#     --agent-alias ALIAS   host agent image alias  (default agent-vm/2026.10.0)
#     --node-alias ALIAS    per-node agent alias    (default agent-os/w4)
#     --cache-dir DIR       on-node image cache dir (default /var/lib/termini/images)
#     --host-cache DIR      host export cache dir   (default $HOME/.cache/termini/images)
#     --pool NAME           nested storage pool     (default local)
#     --no-host-image       skip caching the host image tarball (agent image only)
#     --no-ensure-lxd       do not initialise the node's nested LXD
#     -h|--help
set -euo pipefail

HOST_ALIAS="${TERMINI_HOST_IMAGE_ALIAS:-agentic-os/w3}"
AGENT_ALIAS="${TERMINI_AGENT_HOST_IMAGE_ALIAS:-agent-vm/2026.10.0}"
NODE_ALIAS="${TERMINI_NODE_IMAGE_ALIAS:-agent-os/w4}"
NODES_STR="${TERMINI_NODES:-demo-node-a demo-node-b demo-node-c}"
CACHE_DIR="${TERMINI_IMAGE_CACHE_DIR:-/var/lib/termini/images}"
HOST_CACHE="${TERMINI_HOST_IMAGE_CACHE:-$HOME/.cache/termini/images}"
POOL="${TERMINI_POOL:-local}"
WANT_HOST_IMAGE=1
ENSURE_LXD=1

log()  { printf '[prewarm] %s\n' "$*" >&2; }
die()  { printf '[prewarm] FATAL: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }
remote() { local n="$1"; shift; lxc exec "$n" -- "$@"; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --nodes) NODES_STR="$2"; shift 2 ;;
    --host-alias) HOST_ALIAS="$2"; shift 2 ;;
    --agent-alias) AGENT_ALIAS="$2"; shift 2 ;;
    --node-alias) NODE_ALIAS="$2"; shift 2 ;;
    --cache-dir) CACHE_DIR="$2"; shift 2 ;;
    --host-cache) HOST_CACHE="$2"; shift 2 ;;
    --pool) POOL="$2"; shift 2 ;;
    --no-host-image) WANT_HOST_IMAGE=0; shift ;;
    --no-ensure-lxd) ENSURE_LXD=0; shift ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

need lxc; need sha256sum; need awk; need jq

host_fp() { lxc image info "$1" 2>/dev/null | awk '/^Fingerprint:/{print $2}'; }
slug() { printf '%s' "$1" | tr '/ ' '--'; }

# Export one host image to a cached tarball (idempotent by content).
host_cache_image() { # ALIAS OUT_TARBALL -> echoes sha256
  local alias="$1" out="$2" dir base
  dir="$(dirname "$out")"; base="$(basename "$out")"
  mkdir -p "$dir"
  if [ -s "$out" ] && [ -s "$out.sha256" ] && ( cd "$dir" && sha256sum -c "$base.sha256" >/dev/null 2>&1 ); then
    log "host cache: $base already exported (digest matches)"
  else
    log "host cache: exporting $alias -> $out"
    lxc image export "$alias" "${out%.tar.gz}" >/dev/null
    [ -s "$out" ] || die "image export produced no tarball for $alias"
    ( cd "$dir" && sha256sum "$base" > "$base.sha256" )
  fi
  awk '{print $1}' "$out.sha256"
}

subnet_for() { # NODE -> bridge CIDR per infra-decisions.md D2 (10.20.1/2/3.0/24)
  case "$1" in
    *a|*a-*) echo "10.20.1.1/24" ;;
    *b|*b-*) echo "10.20.2.1/24" ;;
    *c|*c-*) echo "10.20.3.1/24" ;;
    *)       echo "10.20.9.1/24" ;;
  esac
}

ensure_node_lxd() { # NODE
  local n="$1" subnet; subnet="$(subnet_for "$n")"
  remote "$n" lxc storage show "$POOL" >/dev/null 2>&1 \
    || { log "$n: creating storage pool $POOL (dir)"; remote "$n" lxc storage create "$POOL" dir; }
  remote "$n" lxc network show lxdbr0 >/dev/null 2>&1 \
    || { log "$n: creating network lxdbr0 ($subnet)"; remote "$n" lxc network create lxdbr0 "ipv4.address=$subnet" ipv4.nat=true ipv6.address=none; }
  remote "$n" lxc profile device show default 2>/dev/null | grep -q '^root:' \
    || remote "$n" lxc profile device add default root disk "path=/" "pool=$POOL"
  remote "$n" lxc profile device show default 2>/dev/null | grep -q '^eth0:' \
    || remote "$n" lxc profile device add default eth0 nic name=eth0 network=lxdbr0
}

node_fp() { remote "$1" lxc image info "$2" 2>/dev/null | awk '/^Fingerprint:/{print $2}' || true; }

# Push one tarball to the node's on-disk cache and verify its digest there.
node_cache_image() { # NODE LOCAL_TARBALL EXPECTED_SHA
  local n="$1" src="$2" want="$3" base got
  base="$(basename "$src")"
  remote "$n" mkdir -p "$CACHE_DIR"
  local have=""
  have="$(remote "$n" sha256sum "$CACHE_DIR/$base" 2>/dev/null | awk '{print $1}' || true)"
  if [ "$have" = "$want" ]; then
    log "$n: $base already cached (digest matches)"
  else
    log "$n: pushing $base ($(du -h "$src" | cut -f1))"
    lxc file push "$src" "$n$CACHE_DIR/$base" >/dev/null 2>&1 || die "$n: image push of $base failed"
    got="$(remote "$n" sha256sum "$CACHE_DIR/$base" | awk '{print $1}')"
    [ "$got" = "$want" ] || die "$n: cached $base digest $got != host $want"
  fi
  remote "$n" bash -c "cd '$CACHE_DIR' && sha256sum '$base' > '$base.sha256'"
}

host_fp_want="$(host_fp "$HOST_ALIAS")"
agent_fp_want="$(host_fp "$AGENT_ALIAS")"
[ -n "$agent_fp_want" ] || die "cannot resolve agent image alias $AGENT_ALIAS on this host"
[ "$WANT_HOST_IMAGE" = 0 ] || [ -n "$host_fp_want" ] || die "cannot resolve host image alias $HOST_ALIAS on this host"

host_file="$(slug "$HOST_ALIAS")-${host_fp_want:0:12}.tar.gz"
agent_file="$(slug "$AGENT_ALIAS")-${agent_fp_want:0:12}.tar.gz"

host_sha=""
if [ "$WANT_HOST_IMAGE" = 1 ]; then
  host_sha="$(host_cache_image "$HOST_ALIAS" "$HOST_CACHE/$host_file")"
fi
agent_sha="$(host_cache_image "$AGENT_ALIAS" "$HOST_CACHE/$agent_file")"
image_version="$(lxc image list "$AGENT_ALIAS" --format json | jq -r '.[0].properties.version // "unknown"')"

for n in $NODES_STR; do
  log "=== node $n ==="
  lxc info "$n" >/dev/null 2>&1 || die "node $n not found in this LXD"

  [ "$ENSURE_LXD" = 1 ] && ensure_node_lxd "$n"

  # on-disk tarball cache (the artifacts D3 imports from)
  [ "$WANT_HOST_IMAGE" = 0 ] || node_cache_image "$n" "$HOST_CACHE/$host_file" "$host_sha"
  node_cache_image "$n" "$HOST_CACHE/$agent_file" "$agent_sha"

  # import the agent image into the node's LOCAL store from the cached tarball
  cur_fp="$(node_fp "$n" "$NODE_ALIAS")"
  if [ "$cur_fp" = "$agent_fp_want" ]; then
    log "$n: alias $NODE_ALIAS already present (fingerprint ${cur_fp:0:12}) — no import needed"
  else
    [ -n "$cur_fp" ] && { log "$n: replacing stale alias ($cur_fp)"; remote "$n" lxc image delete "$NODE_ALIAS"; }
    log "$n: importing $agent_file into the local store as $NODE_ALIAS"
    remote "$n" lxc image import "$CACHE_DIR/$agent_file" --alias "$NODE_ALIAS"
  fi

  # machine-readable residency manifest on the node
  cat > "/tmp/.prewarm-cache-$$.json" <<EOF
{
  "kind": "termini.image_cache",
  "node": "$n",
  "host_image": {
    "alias": "$HOST_ALIAS",
    "fingerprint": "$host_fp_want",
    "file": "$host_file",
    "sha256": "$host_sha"
  },
  "agent_image": {
    "alias": "$AGENT_ALIAS",
    "version": "$image_version",
    "fingerprint": "$agent_fp_want",
    "node_alias": "$NODE_ALIAS",
    "file": "$agent_file",
    "sha256": "$agent_sha"
  }
}
EOF
  lxc file push "/tmp/.prewarm-cache-$$.json" "$n$CACHE_DIR/cache.json" >/dev/null
  rm -f "/tmp/.prewarm-cache-$$.json"

  log "$n: cached=[$([ "$WANT_HOST_IMAGE" = 1 ] && printf '%s ' "${host_file:0:40}")${agent_file:0:40}] alias=$NODE_ALIAS@${agent_fp_want:0:12}"
done

log "done: $NODES_STR (cache dir $CACHE_DIR on each node; host export cache $HOST_CACHE)"
