#!/usr/bin/env bash
# mtu-probe.sh — DF-bit path-MTU probe for a Termini inference network path.
#
# Part of kanban t_bcae0231. It sends ICMP with the Don't-Fragment bit set so a
# packet larger than the path MTU is dropped in place instead of being silently
# fragmented — which is exactly how an OVN/fan encapsulation MTU mismatch shows
# up as a stalled llama.cpp prompt.
#
#   ./mtu-probe.sh --path direct --expect pass     # 1472-byte payload (MTU 1500)
#   ./mtu-probe.sh --path ovn    --expect fail     # same payload, overlay MTU 1450
#   ./mtu-probe.sh --path ovn    --mtu 1450        # probe at the tunnel size
#   ./mtu-probe.sh --path ovn    --sweep           # binary-search the real path MTU
#
# Exit 0 when the observed result matches --expect (default: pass).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/path-env.sh
. "${HERE}/lib/path-env.sh"

PATH_NAME=""
HOST=""
MTU=""
IFACE=""
SOURCE_IP=""
NETNS=""
COUNT=3
EXPECT="pass"
SWEEP=0
SWEEP_MAX=2000
JSON=0

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --path) PATH_NAME="${2:?}"; shift 2 ;;
    --host) HOST="${2:?}"; shift 2 ;;
    --mtu) MTU="${2:?}"; shift 2 ;;
    --interface) IFACE="${2:?}"; shift 2 ;;
    --source-ip) SOURCE_IP="${2:?}"; shift 2 ;;
    --netns) NETNS="${2:?}"; shift 2 ;;
    --count) COUNT="${2:?}"; shift 2 ;;
    --expect) EXPECT="${2:?}"; shift 2 ;;
    --sweep) SWEEP=1; shift ;;
    --sweep-max) SWEEP_MAX="${2:?}"; shift 2 ;;
    --json) JSON=1; shift ;;
    -h | --help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 1 ;;
  esac
done

if [ -n "$PATH_NAME" ]; then
  termini_path_valid "$PATH_NAME" || { echo "unknown path: $PATH_NAME (want direct|ovn)" >&2; exit 2; }
  # Explicit --host/--mtu still win over the config for this invocation.
  [ -n "$HOST" ] || HOST="$(termini_path_field "$PATH_NAME" host "10.10.0.13")"
  [ -n "$MTU" ] || MTU="$(termini_path_mtu "$PATH_NAME")"
  [ -n "$IFACE" ] || IFACE="$(termini_path_interface "$PATH_NAME")"
else
  [ -n "$HOST" ] || HOST="$(termini_path_field "$(termini_active_path)" host "10.10.0.13")"
  [ -n "$MTU" ] || MTU="$(termini_path_mtu "$(termini_active_path)")"
fi

# Run ping, optionally inside a netns, with an optional source/interface pin.
run_ping() {
  local payload="$1"
  local -a cmd=(ping -M 'do' -s "$payload" -c "$COUNT" -W 2)
  [ -n "$IFACE" ] && cmd+=(-I "$IFACE")
  [ -n "$SOURCE_IP" ] && cmd+=(-S "$SOURCE_IP")
  cmd+=("$HOST")
  if [ -n "$NETNS" ]; then
    ip netns exec "$NETNS" "${cmd[@]}" 2>&1
  else
    "${cmd[@]}" 2>&1
  fi
  return 0
}

# pass = the probe reached the host at this size; fail = DF packet was dropped.
probe_passes() {
  local payload="$1" out
  out="$(run_ping "$payload")" || true
  if printf '%s\n' "$out" | grep -qiE 'too long|Message too long|Frag needed'; then
    return 1
  fi
  # Any received reply with no loss at this size counts as passing.
  printf '%s\n' "$out" | grep -qiE '([1-9][0-9]*) received|bytes from' || return 1
  printf '%s\n' "$out" | grep -qiE '100% packet loss' && return 1
  return 0
}

if [ "$SWEEP" -eq 1 ]; then
  # Binary-search the largest DF payload that survives; path MTU = payload + 28.
  lo=28
  hi="$SWEEP_MAX"
  best=0
  while [ "$lo" -le "$hi" ]; do
    mid=$(( (lo + hi) / 2 ))
    if probe_passes "$mid"; then
      best="$mid"
      lo=$((mid + 1))
    else
      hi=$((mid - 1))
    fi
  done
  mtu_result=$((best + 28))
  if [ "$JSON" -eq 1 ]; then
    jq -nc --arg host "$HOST" --argjson payload "$best" --argjson mtu "$mtu_result" \
      '{host:$host, max_df_payload:$payload, path_mtu:$mtu, method:"binary-search"}'
  else
    echo "path-mtu sweep ${HOST}: max DF payload=${best} bytes -> path MTU=${mtu_result}"
  fi
  exit 0
fi

payload=$((MTU - 28))
out="$(run_ping "$payload")" || true
if printf '%s\n' "$out" | grep -qiE 'too long|Frag needed|100% packet loss'; then
  observed="fail"
elif printf '%s\n' "$out" | grep -qiE '([1-9][0-9]*) received|bytes from'; then
  observed="pass"
else
  observed="fail"
fi

if [ "$JSON" -eq 1 ]; then
  jq -nc --arg host "$HOST" --argjson mtu "$MTU" --argjson payload "$payload" \
    --arg observed "$observed" --arg expect "$EXPECT" \
    '{host:$host, path_mtu:$mtu, df_payload:$payload, observed:$observed, expected:$expect, ok:($observed==$expect)}'
else
  echo "probe ${PATH_NAME:-custom} -> ${HOST}: DF payload=${payload} (MTU ${MTU}): ${observed} (expected ${EXPECT})"
fi

[ "$observed" = "$EXPECT" ]
