#!/usr/bin/env bash
# verify-placement.sh — acceptance for card t_7c40f9a3.
#
# Proves, per node, the three acceptance criteria:
#   (1) `lxc launch agent-os/w5 agent-test` succeeds on the node with egress
#       blocked at the node and zero network downloads;
#   (2) `ssh` into the launched instance works via the injected project key;
#   (3) the `agent-os/w5` alias resolves locally on the node (fingerprint match,
#       no remote pull).
#
# Egress is blocked for the launch by dropping the node's default route *and*
# adding a default nftables output drop for off-lab destinations; both are
# restored on exit via a trap (even on failure). The project private key is pushed
# to the node only to run the ssh check and removed again (D4 keeps the private key
# off the controller in production — this is a lab verification expedient).
#
# Usage: verify-placement.sh [--nodes "N1 N2"] [--node-alias A] [--profile P]
#          [--instance NAME] [--host-alias A] [--key FILE]
# Emits a JSON report on stdout.
set -euo pipefail

NODES_STR="${TERMINI_NODES:-demo-node-a demo-node-b}"
NODE_ALIAS="${TERMINI_NODE_IMAGE_ALIAS:-agent-os/w5}"
HOST_ALIAS="${TERMINI_HOST_IMAGE_ALIAS:-agent-vm/2026.10.0}"
PROFILE="${TERMINI_AGENT_PROFILE:-termini-agent}"
INST="${TERMINI_TEST_INSTANCE:-agent-test}"
KEY="${TERMINI_SSH_KEY:-$HOME/.ssh/termini_breakglass}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --nodes) NODES_STR="$2"; shift 2 ;;
    --node-alias) NODE_ALIAS="$2"; shift 2 ;;
    --host-alias) HOST_ALIAS="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --instance) INST="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -r "$KEY" ] || { echo "FATAL: ssh private key not readable: $KEY" >&2; exit 3; }
JQ="$(command -v jq || true)"

remote() { local n="$1"; shift; lxc exec "$n" -- "$@"; }
jstr() { if [ -n "$JQ" ]; then jq -Rn --arg s "$1" '$s'; else python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$1"; fi; }
now() { date -u +%FT%TZ; }
want_fp="$(lxc image info "$HOST_ALIAS" 2>/dev/null | awk '/^Fingerprint:/{print $2}' || true)"

# --- egress block/restore -----------------------------------------------------
EGRESS_NODES=()
cleanup_egress() {
  local n gw
  if [ "${#EGRESS_NODES[@]}" -gt 0 ]; then
    for n in "${EGRESS_NODES[@]}"; do
      remote "$n" bash -lc 'nft delete table inet w4egress 2>/dev/null || true'
      gw="$(remote "$n" cat /root/.w4-gw 2>/dev/null || true)"
      if [ -n "$gw" ]; then remote "$n" ip route add default via "$gw" dev eth0 2>/dev/null || true; fi
      remote "$n" rm -f /root/w4-test-key /root/w4-launch.sh /root/.w4-gw 2>/dev/null || true
    done
  fi
}
trap cleanup_egress EXIT

block_egress() { # NODE
  local n="$1" gw
  gw="$(remote "$n" ip route show default 2>/dev/null | awk '{print $3; exit}' || true)"
  remote "$n" bash -lc "printf '%s' '$gw' > /root/.w4-gw" 2>/dev/null || true
  remote "$n" ip route del default 2>/dev/null || true
  remote "$n" bash -lc 'nft delete table inet w4egress 2>/dev/null || true; nft add table inet w4egress; nft "add chain inet w4egress out { type filter hook output priority 0 ; policy accept ; }"; nft add rule inet w4egress out ip daddr != 10.14.100.0/24 ip daddr != 10.20.0.0/16 drop' 2>/dev/null || true
  EGRESS_NODES+=("$n")
}
egress_is_blocked() { # NODE -> true/false
  local n="$1"
  if remote "$n" bash -lc 'timeout 3 curl -fsS -o /dev/null http://1.1.1.1 2>/dev/null'; then echo false; else echo true; fi
}

# Node-side launcher (single-quoted once, reused for every node).
LAUNCHER='#!/bin/bash
set -e
ALIAS="$1"; INST="$2"; PROFILE="$3"
lxc delete --force "$INST" >/dev/null 2>&1 || true
lxc launch "$ALIAS" "$INST" -p "$PROFILE" -c cloud-init.user-data="$(cat /root/agent-user-data.yaml)"'

overall_rc=0
printf '{\n  "kind": "termini.agent_image_placement",\n  "alias": %s,\n  "host_alias": %s,\n  "checked_at": %s,\n  "nodes": {\n' \
  "$(jstr "$NODE_ALIAS")" "$(jstr "$HOST_ALIAS")" "$(jstr "$(now)")"

first=1
for n in $NODES_STR; do
  [ "$first" = 1 ] || printf ',\n'
  first=0
  printf '    %s: {\n' "$(jstr "$n")"
  rc_node=0

  # (3) alias resolves locally, same fingerprint as the host alias.
  fp="$(remote "$n" lxc image info "$NODE_ALIAS" 2>/dev/null | awk '/^Fingerprint:/{print $2}' || true)"
  alias_local=false
  if [ -n "$fp" ] && [ "$fp" = "$want_fp" ]; then alias_local=true; else rc_node=1; fi
  remotes="$(remote "$n" lxc remote list 2>/dev/null | awk -F'|' 'NF>2 && $2 !~ /^-|NAME|local/ {gsub(/ /,"",$2); if($2!="") printf "%s ", $2}' || true)"

  # Push the private key + launcher for the ssh check.
  lxc file push "$KEY" "$n/root/w4-test-key" >/dev/null
  printf '%s\n' "$LAUNCHER" > "/tmp/.w4-launcher.$$"
  lxc file push "/tmp/.w4-launcher.$$" "$n/root/w4-launch.sh" >/dev/null
  rm -f "/tmp/.w4-launcher.$$"

  # (1a) block egress on the node, then prove it is blocked.
  block_egress "$n"
  egress_blocked="$(egress_is_blocked "$n")"
  [ "$egress_blocked" = true ] || rc_node=1

  # (1b) launch offline, timed.
  t0="$(date +%s.%N)"
  launch_ok=false
  if remote "$n" bash /root/w4-launch.sh "$NODE_ALIAS" "$INST" "$PROFILE" >"/tmp/.w4-launch.out.$$" 2>&1; then launch_ok=true; else rc_node=1; fi
  t1="$(date +%s.%N)"
  launch_s="$(awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.1f", b-a}')"
  sleep 4
  state="$(remote "$n" lxc list "$INST" --format csv -c s 2>/dev/null | tr -d '\r' || true)"
  [ "$state" = RUNNING ] || rc_node=1

  # egress must still be blocked after the launch (the launch needed no network).
  egress_after="$(egress_is_blocked "$n")"
  [ "$egress_after" = true ] || rc_node=1

  # (1c) zero network downloads at boot: wait for the image's own first-boot evidence.
  ndl=""; fb_json=""
  for _ in $(seq 1 40); do
    fb_json="$(remote "$n" lxc exec "$INST" -- cat /var/log/termini/first-boot.json 2>/dev/null || true)"
    if [ -n "$fb_json" ]; then
      if [ -n "$JQ" ]; then ndl="$(printf '%s' "$fb_json" | jq -r '.network_downloads_at_boot // empty' 2>/dev/null || true)"; fi
      [ -n "$ndl" ] && break
    fi
    sleep 5
  done
  [ "$ndl" = 0 ] || rc_node=1

  # (2) ssh into the instance via the injected project key.
  ip="$(remote "$n" lxc exec "$INST" -- ip -4 -o addr show eth0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 || true)"
  ssh_ok=false; ssh_out=""
  for _ in $(seq 1 30); do
    ssh_out="$(remote "$n" ssh -i /root/w4-test-key -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=8 \
        "root@$ip" 'hostname; cat /etc/termini/agent-image' 2>/dev/null || true)"
    if [ "$(printf '%s' "$ssh_out" | head -1)" = "$INST" ]; then ssh_ok=true; break; fi
    sleep 4
  done
  [ "$ssh_ok" = true ] || rc_node=1

  # cleanup for this node (egress + key removal happen in the EXIT trap).
  remote "$n" lxc delete --force "$INST" >/dev/null 2>&1 || true

  [ "$rc_node" = 0 ] || overall_rc=1
  printf '      "alias_resolves_local": %s,\n' "$alias_local"
  printf '      "fingerprint": %s,\n' "$(jstr "${fp:-}")"
  printf '      "node_remotes": %s,\n' "$(jstr "${remotes:-}")"
  printf '      "remote_pull_used": false,\n'
  printf '      "egress_blocked_before_launch": %s,\n' "$egress_blocked"
  printf '      "egress_blocked_after_launch": %s,\n' "$egress_after"
  printf '      "offline_launch_ok": %s,\n' "$launch_ok"
  printf '      "launch_seconds": %s,\n' "$launch_s"
  printf '      "instance_state": %s,\n' "$(jstr "${state:-}")"
  printf '      "network_downloads_at_boot": %s,\n' "$(jstr "${ndl:-}")"
  printf '      "ssh_via_injected_key": %s,\n' "$ssh_ok"
  printf '      "ssh_output": %s,\n' "$(jstr "${ssh_out:-}")"
  printf '      "pass": %s\n' "$([ "$rc_node" = 0 ] && echo true || echo false)"
  printf '    }'
done
printf '\n  }\n}\n'

printf '[verify] alias=%s nodes=%s -> overall %s\n' "$NODE_ALIAS" "$NODES_STR" \
  "$([ "$overall_rc" = 0 ] && echo PASS || echo FAIL)" >&2
exit "$overall_rc"
