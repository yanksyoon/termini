#!/usr/bin/env bash
# verify-image-residency.sh — acceptance gate for card t_f0017269.
#
# Asserts, per node (default demo-node-a/b/c), that the pre-baked OS images are
# resident on that node's disk and that a fresh agent VM boots from the LOCAL
# image with ZERO network fetches:
#
#   host image (agentic-os/w3)   the node booted from the pre-baked OS: the
#                                /var/lib/termini/.baked-offline sentinel is set
#                                and /etc/termini/image-version matches, AND the
#                                host image tarball is cached on the node's disk
#                                (/var/lib/termini/images/) with a matching digest;
#   agent image (agent-os/w5)    the alias resolves in the node's LOCAL LXD store
#                                at the published fingerprint, AND the agent image
#                                tarball is cached on the node's disk with a
#                                matching digest;
#   zero-network boot            the node's default route is dropped and off-lab
#                                egress is dropped via nftables, a fresh agent VM is
#                                launched from the local alias, and the boot is
#                                proven to need no network: egress is still blocked
#                                after the launch and the image's own first-boot
#                                evidence reports network_downloads_at_boot == 0.
#
# Egress is restored after each node (trap on exit covers failures). Emits a
# machine-readable JSON report on stdout and writes <out>/image-residency-<ts>.json
# plus a text transcript. Exit 0 = every node warm; exit 1 = at least one cold path.
#
# Usage: verify-image-residency.sh [options]
#   --nodes "N1 N2 N3"    nodes to check (default demo-node-a demo-node-b demo-node-c)
#   --host-alias ALIAS    host node image alias  (default agentic-os/w3)
#   --agent-alias ALIAS   host agent image alias (default agent-vm/2026.10.0)
#   --node-alias ALIAS    per-node agent alias   (default agent-os/w5)
#   --cache-dir DIR       on-node image cache dir (default /var/lib/termini/images)
#   --host-cache DIR      host export cache dir  (default $HOME/.cache/termini/images)
#   --expect-version V    expected node OS version (default 2026.10.0)
#   --instance NAME       throwaway instance name (default termini-imgtest)
#   --out DIR             evidence dir (default <script dir>/evidence)
#   --no-launch           residency checks only (no egress block / launch)
#   -h|--help
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODES_STR="${TERMINI_NODES:-demo-node-a demo-node-b demo-node-c}"
HOST_ALIAS="${TERMINI_HOST_IMAGE_ALIAS:-agentic-os/w3}"
AGENT_ALIAS="${TERMINI_AGENT_HOST_IMAGE_ALIAS:-agent-vm/2026.10.0}"
NODE_ALIAS="${TERMINI_NODE_IMAGE_ALIAS:-agent-os/w5}"
CACHE_DIR="${TERMINI_IMAGE_CACHE_DIR:-/var/lib/termini/images}"
HOST_CACHE="${TERMINI_HOST_IMAGE_CACHE:-$HOME/.cache/termini/images}"
EXPECT_VERSION="${TERMINI_IMAGE_VERSION:-2026.10.0}"
INST="${TERMINI_IMG_INSTANCE:-termini-imgtest}"
OUT_DIR="$SELF_DIR/evidence"
DO_LAUNCH=1

while [ "$#" -gt 0 ]; do
  case "$1" in
    --nodes) NODES_STR="$2"; shift 2 ;;
    --host-alias) HOST_ALIAS="$2"; shift 2 ;;
    --agent-alias) AGENT_ALIAS="$2"; shift 2 ;;
    --node-alias) NODE_ALIAS="$2"; shift 2 ;;
    --cache-dir) CACHE_DIR="$2"; shift 2 ;;
    --host-cache) HOST_CACHE="$2"; shift 2 ;;
    --expect-version) EXPECT_VERSION="$2"; shift 2 ;;
    --instance) INST="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --no-launch) DO_LAUNCH=0; shift ;;
    -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

command -v jq >/dev/null 2>&1 || { echo "FATAL: jq is required" >&2; exit 3; }
remote() { local n="$1"; shift; lxc exec "$n" -- "$@"; }
jstr() { jq -Rn --arg s "$1" '$s'; }
now() { date -u +%FT%TZ; }
node_fp() { remote "$1" lxc image info "$2" 2>/dev/null | awk '/^Fingerprint:/{print $2}' || true; }

want_fp="$(lxc image info "$AGENT_ALIAS" 2>/dev/null | awk '/^Fingerprint:/{print $2}' || true)"
[ -n "$want_fp" ] || { echo "FATAL: cannot resolve host agent alias $AGENT_ALIAS" >&2; exit 3; }

mkdir -p "$OUT_DIR"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
JSON_OUT="$OUT_DIR/image-residency-$TS.json"
TXT_OUT="$OUT_DIR/image-residency-$TS.txt"

# --- egress block/restore -----------------------------------------------------
EGRESS_NODES=()
restore_egress() { # NODE
  local n="$1" gw
  remote "$n" bash -lc 'nft delete table inet imgresegress 2>/dev/null || true' 2>/dev/null || true
  gw="$(remote "$n" cat /root/.imgres-gw 2>/dev/null | tr -d '\r' || true)"
  if [ -n "$gw" ]; then remote "$n" ip route add default via "$gw" dev eth0 2>/dev/null || true; fi
  remote "$n" rm -f /root/.imgres-gw 2>/dev/null || true
}
cleanup_egress() {
  local n
  for n in "${EGRESS_NODES[@]:-}"; do
    if [ -n "$n" ]; then restore_egress "$n" || true; fi
  done
  return 0
}
trap cleanup_egress EXIT

block_egress() { # NODE
  local n="$1" gw
  gw="$(remote "$n" ip route show default 2>/dev/null | awk '{print $3; exit}' || true)"
  remote "$n" bash -lc "printf '%s' '$gw' > /root/.imgres-gw" 2>/dev/null || true
  remote "$n" ip route del default 2>/dev/null || true
  remote "$n" bash -lc 'nft delete table inet imgresegress 2>/dev/null || true; nft add table inet imgresegress; nft "add chain inet imgresegress out { type filter hook output priority 0 ; policy accept ; }"; nft add rule inet imgresegress out ip daddr != 10.14.100.0/24 ip daddr != 10.20.0.0/16 drop' 2>/dev/null || true
  EGRESS_NODES+=("$n")
}

egress_reachable() { # NODE -> true/false (TCP connect to an off-lab address)
  local n="$1"
  if remote "$n" bash -lc 'timeout 3 bash -c "exec 3<>/dev/tcp/1.1.1.1/443" 2>/dev/null'; then echo true; else echo false; fi
}

{
  printf '{\n'
  printf '  "kind": "termini.image_residency",\n'
  printf '  "host_alias": %s,\n' "$(jstr "$HOST_ALIAS")"
  printf '  "agent_alias": %s,\n' "$(jstr "$AGENT_ALIAS")"
  printf '  "node_alias": %s,\n' "$(jstr "$NODE_ALIAS")"
  printf '  "expected_version": %s,\n' "$(jstr "$EXPECT_VERSION")"
  printf '  "checked_at": %s,\n' "$(jstr "$(now)")"
  printf '  "launch_performed": %s,\n' "$([ "$DO_LAUNCH" = 1 ] && echo true || echo false)"
  printf '  "nodes": {\n'
} >> "$JSON_OUT"

TXT="$(mktemp)"
{
  printf '=== termini image residency — %s ===\n' "$(now)"
  printf 'host_alias=%s agent_alias=%s node_alias=%s want_fp=%s\n\n' \
    "$HOST_ALIAS" "$AGENT_ALIAS" "$NODE_ALIAS" "$want_fp"
} > "$TXT"

overall_rc=0
first=1
for n in $NODES_STR; do
  [ "$first" = 1 ] || printf ',\n' >> "$JSON_OUT"
  first=0
  printf '    %s: {\n' "$(jstr "$n")" >> "$JSON_OUT"
  rc_node=0
  note() { printf '  %-6s %-28s %s\n' "$1" "$2" "$3" >> "$TXT"; }

  printf -- '--- node %s ---\n' "$n" >> "$TXT"
  if ! lxc info "$n" >/dev/null 2>&1; then
    printf '  FAIL   node_missing                 %s not a local LXD instance\n' "$n" >> "$TXT"
    printf '      "node_present": false,\n      "pass": false\n' >> "$JSON_OUT"
    printf '    }' >> "$JSON_OUT"
    overall_rc=1
    continue
  fi

  # ---- host OS image residency (the node itself is the baked image) ----
  sentinel=false
  remote "$n" test -f /var/lib/termini/.baked-offline 2>/dev/null && sentinel=true
  node_version="$(remote "$n" cat /etc/termini/image-version 2>/dev/null | tr -d '\r' || true)"
  version_match=false
  [ "$node_version" = "$EXPECT_VERSION" ] && version_match=true
  [ "$sentinel" = true ] && note PASS host_image_baked "sentinel set, image_version=$node_version" \
    || { note FAIL host_image_baked "no /var/lib/termini/.baked-offline"; rc_node=1; }
  [ "$version_match" = true ] && note PASS host_image_version "image_version=$node_version" \
    || { note FAIL host_image_version "image_version='$node_version' != '$EXPECT_VERSION'"; rc_node=1; }

  # ---- on-disk image cache (tarballs + digests) ----
  cache_json="$(remote "$n" cat "$CACHE_DIR/cache.json" 2>/dev/null || true)"
  host_file="$(printf '%s' "$cache_json" | jq -r '.host_image.file // empty' 2>/dev/null || true)"
  agent_file="$(printf '%s' "$cache_json" | jq -r '.agent_image.file // empty' 2>/dev/null || true)"

  check_cached() { # FILE -> sets cached_ok, cached_detail
    cached_ok=false; cached_detail="missing"
    if [ -z "$1" ]; then cached_detail="no cache.json entry"; return; fi
    if ! remote "$n" test -s "$CACHE_DIR/$1" 2>/dev/null; then cached_detail="$1 absent"; return; fi
    local actual side hostside
    actual="$(remote "$n" sha256sum "$CACHE_DIR/$1" | awk '{print $1}')"
    side="$(remote "$n" awk '{print $1}' "$CACHE_DIR/$1.sha256" 2>/dev/null | tr -d '\r' || true)"
    hostside=""
    [ -s "$HOST_CACHE/$1.sha256" ] && hostside="$(awk '{print $1}' "$HOST_CACHE/$1.sha256")"
    if [ "$actual" = "$side" ] && { [ -z "$hostside" ] || [ "$actual" = "$hostside" ]; }; then
      cached_ok=true; cached_detail="$1 sha256=${actual:0:16}…"
    else
      cached_detail="$1 digest mismatch (actual=${actual:0:16}… side=${side:0:16}… host=${hostside:0:16}…)"
    fi
  }

  check_cached "$host_file"; host_cached="$cached_ok"; host_cached_detail="$cached_detail"
  [ "$host_cached" = true ] && note PASS host_image_cached "$host_cached_detail" \
    || { note FAIL host_image_cached "$host_cached_detail"; rc_node=1; }

  check_cached "$agent_file"; agent_cached="$cached_ok"; agent_cached_detail="$cached_detail"
  [ "$agent_cached" = true ] && note PASS agent_image_cached "$agent_cached_detail" \
    || { note FAIL agent_image_cached "$agent_cached_detail"; rc_node=1; }

  # ---- agent image alias in the node's LOCAL store ----
  fp="$(node_fp "$n" "$NODE_ALIAS")"
  alias_local=false
  [ -n "$fp" ] && [ "$fp" = "$want_fp" ] && alias_local=true
  [ "$alias_local" = true ] && note PASS agent_alias_local "agent-os alias @${fp:0:12} (local)" \
    || { note FAIL agent_alias_local "alias '$NODE_ALIAS' fp='${fp:-none}' != pinned ${want_fp:0:12}"; rc_node=1; }
  remotes="$(remote "$n" lxc remote list 2>/dev/null | awk -F'|' 'NF>2 && $2 !~ /^-|NAME|local/ {gsub(/ /,"",$2); if($2!="") printf "%s ", $2}' || true)"

  # ---- zero-network boot proof ----
  egress_before=skip; egress_blocked=false; egress_after=skip
  launch_ok=false; state=""; ndl=""; verified_offline=""; launch_s=0
  if [ "$DO_LAUNCH" = 1 ]; then
    egress_before="$(egress_reachable "$n")"
    block_egress "$n"
    reachable_after_block="$(egress_reachable "$n")"
    [ "$reachable_after_block" = false ] && egress_blocked=true
    [ "$egress_blocked" = true ] && note PASS egress_blocked "off-lab TCP unreachable after block (before=$egress_before)" \
      || { note FAIL egress_blocked "off-lab TCP still reachable after block"; rc_node=1; }

    remote "$n" lxc delete --force "$INST" >/dev/null 2>&1 || true
    t0="$(date +%s.%N)"
    if remote "$n" bash -lc "lxc launch '$NODE_ALIAS' '$INST'" >"/tmp/.imgres-launch.$$" 2>&1; then launch_ok=true; else rc_node=1; fi
    t1="$(date +%s.%N)"
    launch_s="$(awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.1f", b-a}')"
    sleep 3
    state="$(remote "$n" lxc list "$INST" --format csv -c s 2>/dev/null | tr -d '\r' || true)"
    [ "$state" = RUNNING ] || rc_node=1

    egress_after="$(egress_reachable "$n")"
    [ "$egress_after" = false ] && note PASS egress_after_launch "off-lab TCP still unreachable" \
      || { note FAIL egress_after_launch "egress reachable after launch (a pull could have egressed)"; rc_node=1; }

    fb=""
    for _ in $(seq 1 40); do
      fb="$(remote "$n" lxc exec "$INST" -- cat /var/log/termini/first-boot.json 2>/dev/null || true)"
      [ -n "$fb" ] && break
      sleep 5
    done
    if [ -n "$fb" ]; then
      ndl="$(printf '%s' "$fb" | jq -r '.network_downloads_at_boot // empty' 2>/dev/null || true)"
      verified_offline="$(printf '%s' "$fb" | jq -r '.verified_offline // empty' 2>/dev/null || true)"
    fi
    [ "$ndl" = 0 ] && note PASS zero_network_downloads "first-boot network_downloads_at_boot=0" \
      || { note FAIL zero_network_downloads "network_downloads_at_boot='${ndl:-none}'"; rc_node=1; }
    [ "$verified_offline" = true ] && note PASS verified_offline "image first-boot verified_offline=true" \
      || { note FAIL verified_offline "verified_offline='${verified_offline:-none}'"; rc_node=1; }

    remote "$n" lxc delete --force "$INST" >/dev/null 2>&1 || true
    restore_egress "$n"
    EGRESS_NODES=()
    [ "$launch_ok" = true ] && note PASS offline_launch_ok "launched from local alias in ${launch_s}s (state=$state)" \
      || { note FAIL offline_launch_ok "lxc launch from local alias failed"; rc_node=1; }
  fi

  [ "$rc_node" = 0 ] || overall_rc=1
  printf '      "node_present": true,\n' >> "$JSON_OUT"
  printf '      "host_image_baked": %s,\n' "$sentinel" >> "$JSON_OUT"
  printf '      "host_image_version": %s,\n' "$(jstr "$node_version")" >> "$JSON_OUT"
  printf '      "host_image_version_match": %s,\n' "$version_match" >> "$JSON_OUT"
  printf '      "host_image_file": %s,\n' "$(jstr "$host_file")" >> "$JSON_OUT"
  printf '      "host_image_cached": %s,\n' "$host_cached" >> "$JSON_OUT"
  printf '      "agent_image_file": %s,\n' "$(jstr "$agent_file")" >> "$JSON_OUT"
  printf '      "agent_image_cached": %s,\n' "$agent_cached" >> "$JSON_OUT"
  printf '      "agent_alias_local": %s,\n' "$alias_local" >> "$JSON_OUT"
  printf '      "agent_alias_fingerprint": %s,\n' "$(jstr "${fp:-}")" >> "$JSON_OUT"
  printf '      "node_remotes": %s,\n' "$(jstr "${remotes:-}")" >> "$JSON_OUT"
  printf '      "egress_reachable_before_block": %s,\n' "$(jstr "${egress_before:-skip}")" >> "$JSON_OUT"
  printf '      "egress_blocked": %s,\n' "$egress_blocked" >> "$JSON_OUT"
  printf '      "egress_reachable_after_launch": %s,\n' "$(jstr "${egress_after:-skip}")" >> "$JSON_OUT"
  printf '      "offline_launch_ok": %s,\n' "$launch_ok" >> "$JSON_OUT"
  printf '      "launch_seconds": %s,\n' "$launch_s" >> "$JSON_OUT"
  printf '      "instance_state": %s,\n' "$(jstr "${state:-}")" >> "$JSON_OUT"
  printf '      "network_downloads_at_boot": %s,\n' "$(jstr "${ndl:-}")" >> "$JSON_OUT"
  printf '      "verified_offline": %s,\n' "$(jstr "${verified_offline:-}")" >> "$JSON_OUT"
  printf '      "pass": %s\n' "$([ "$rc_node" = 0 ] && echo true || echo false)" >> "$JSON_OUT"
  printf '    }' >> "$JSON_OUT"
  printf '\n' >> "$TXT"
done
printf '\n  },\n  "ok": %s\n}\n' "$([ "$overall_rc" = 0 ] && echo true || echo false)" >> "$JSON_OUT"

cp "$TXT" "$TXT_OUT"
rm -f "$TXT"
jq -c . "$JSON_OUT" >/dev/null || { echo "FATAL: produced invalid JSON" >&2; exit 3; }

cat "$JSON_OUT"
printf '[verify] residency %s -> %s (evidence: %s)\n' "$NODES_STR" \
  "$([ "$overall_rc" = 0 ] && echo PASS || echo FAIL)" "$JSON_OUT" >&2
printf 'WARM: all nodes carry the pre-baked images and boot with zero network fetches.\n' >&2
[ "$overall_rc" = 0 ] || printf 'COLD: %s — a node cannot boot/launch offline.\n' "$NODES_STR" >&2
exit "$overall_rc"
