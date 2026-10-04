#!/usr/bin/env bash
# overlay-vs-direct-lab.sh — reproducible proof that the direct route bypasses
# the overlay MTU penalty and that an overlay black hole stalls llama.cpp.
#
# Part of kanban t_bcae0231. Builds a two-path topology entirely in network
# namespaces (no external hosts, no OVS/OVN required) and runs the card's
# acceptance checks against it:
#
#   direct   : agent <-> inference over a plain routed veth at the underlay MTU
#   overlay  : the same two endpoints connected over a real VXLAN tunnel, whose
#              device MTU is 50 bytes smaller than the underlay (1450 vs 1500)
#   blackhole: a third topology modelling an overlay whose path MTU is smaller
#              than the endpoints believe AND whose ICMP frag-needed is filtered
#              — the classic PMTU black hole that makes a prompt upload stall.
#
# Run as root:   sudo infra/net/lab/overlay-vs-direct-lab.sh
#   --quick      shorter load-test durations (smoke test)
#   --out DIR    evidence directory (default infra/net/evidence)
#
# It is destructive only to namespaces/veths it owns (prefix tnlab-).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NET_DIR="$(cd "${HERE}/.." && pwd)"
# shellcheck source=../lib/path-env.sh
. "${NET_DIR}/lib/path-env.sh"

# ---- tunables ---------------------------------------------------------------
NS_A="tnlab-a"      # agent / Node A
NS_C="tnlab-c"      # inference / Node C
NS_H="tnlab-h"      # overlay hop for the black-hole topology
A_UNDERLAY="10.10.0.11"
C_UNDERLAY="10.10.0.13"
A_OVERLAY="10.99.0.11"
C_OVERLAY="10.99.0.13"
BH_A="10.31.0.2"
BH_H1="10.31.0.1"
BH_H2="10.32.0.1"
BH_C="10.32.0.2"
UNDERLAY_MTU=1500
VXLAN_ID=4242
PORT=8123
PYTHON="/usr/bin/python3"

DIRECT_DURATION=180
OVERLAY_DURATION=60
BLACKHOLE_DURATION=30
OUT_DIR="${NET_DIR}/evidence"

# ---- arg parsing ------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --quick) DIRECT_DURATION=30; OVERLAY_DURATION=20; BLACKHOLE_DURATION=15; shift ;;
    --out) OUT_DIR="${2:?}"; shift 2 ;;
    -h | --help) sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root (netns/veth/vxlan); re-run with sudo" >&2
  exit 2
fi

mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_LOG="${OUT_DIR}/lab-run-${STAMP}.txt"
exec > >(tee "$RUN_LOG") 2>&1

cleanup() {
  if [ -n "${SERVER_PID:-}" ]; then
    kill "$SERVER_PID" 2>/dev/null || true
  fi
  for ns in "$NS_A" "$NS_C" "$NS_H"; do
    ip netns del "$ns" 2>/dev/null || true
  done
}
trap cleanup EXIT

fail_count=0
record() { printf '%s\n' "$*" >>"/tmp/tnlab-summary.$$"; }

echo "=============================================================="
echo " Termini direct-vs-overlay network lab   ${STAMP}"
echo "=============================================================="

# ---- topology ---------------------------------------------------------------
cleanup
ip netns add "$NS_A"
ip netns add "$NS_C"
ip netns add "$NS_H"
ip -n "$NS_A" link set lo up
ip -n "$NS_C" link set lo up
ip -n "$NS_H" link set lo up

# underlay: the direct path, MTU 1500
ip link add ula netns "$NS_A" type veth peer name ulc netns "$NS_C"
ip -n "$NS_A" link set ula up mtu "$UNDERLAY_MTU"
ip -n "$NS_C" link set ulc up mtu "$UNDERLAY_MTU"
ip -n "$NS_A" addr add "${A_UNDERLAY}/24" dev ula
ip -n "$NS_C" addr add "${C_UNDERLAY}/24" dev ulc

# overlay: a real VXLAN tunnel over that underlay
ip -n "$NS_A" link add vxa type vxlan id "$VXLAN_ID" dev ula \
  remote "$C_UNDERLAY" local "$A_UNDERLAY" dstport 4789
ip -n "$NS_C" link add vxc type vxlan id "$VXLAN_ID" dev ulc \
  remote "$A_UNDERLAY" local "$C_UNDERLAY" dstport 4789
ip -n "$NS_A" link set vxa up
ip -n "$NS_C" link set vxc up
ip -n "$NS_A" addr add "${A_OVERLAY}/24" dev vxa
ip -n "$NS_C" addr add "${C_OVERLAY}/24" dev vxc

echo
echo "---- interface MTUs ----------------------------------------------------"
direct_mtu="$(ip -n "$NS_A" -o link show ula | grep -oE 'mtu [0-9]+' | awk '{print $2}')"
overlay_mtu="$(ip -n "$NS_A" -o link show vxa | grep -oE 'mtu [0-9]+' | awk '{print $2}')"
echo "direct  path (ula, routed underlay) MTU = ${direct_mtu}"
echo "overlay path (vxa, VXLAN tunnel)    MTU = ${overlay_mtu}"
echo "encap overhead observed (direct - overlay) = $((direct_mtu - overlay_mtu)) bytes"

# ---- mock llama.cpp server in the inference namespace -----------------------
echo
echo "---- starting mock llama.cpp (OpenAI SSE) on ${C_UNDERLAY}:${PORT} ------"
ip netns exec "$NS_C" "$PYTHON" "${HERE}/mock_llama_server.py" \
  --host 0.0.0.0 --port "$PORT" >"${OUT_DIR}/mock-server-${STAMP}.txt" 2>&1 &
SERVER_PID=$!
ready=0
for _ in $(seq 1 40); do
  if ip netns exec "$NS_A" curl -sS -m 2 -o /dev/null "http://${C_UNDERLAY}:${PORT}/health" 2>/dev/null; then
    ready=1
    break
  fi
  sleep 0.25
done
if [ "$ready" -ne 1 ]; then
  echo "FATAL: mock server never became ready" >&2
  exit 1
fi
echo "mock llama.cpp ready (pid ${SERVER_PID})"

# ---- 1. path-MTU / DF-bit probes -------------------------------------------
echo
echo "=============== 1. DF-bit path-MTU probes =============================="
probe() {
  local label="$1" ns="$2" host="$3" mtu="$4" expect="$5"
  if "${NET_DIR}/mtu-probe.sh" --netns "$ns" --host "$host" --mtu "$mtu" --expect "$expect" --count 3; then
    record "PROBE ${label}: OK (${expect})"
  else
    record "PROBE ${label}: UNEXPECTED"
    fail_count=$((fail_count + 1))
  fi
}
probe "direct  @ ${direct_mtu}" "$NS_A" "$C_UNDERLAY" "$direct_mtu" pass
probe "overlay @ ${direct_mtu} (direct MTU)" "$NS_A" "$C_OVERLAY" "$direct_mtu" fail
probe "overlay @ ${overlay_mtu} (tunnel MTU)" "$NS_A" "$C_OVERLAY" "$overlay_mtu" pass
probe "direct  @ ${overlay_mtu}" "$NS_A" "$C_UNDERLAY" "$overlay_mtu" pass

echo
echo "---- PMTU sweep (binary search for the real path MTU) ------------------"
"${NET_DIR}/mtu-probe.sh" --netns "$NS_A" --host "$C_UNDERLAY" --sweep --sweep-max 2000 | tee -a "${OUT_DIR}/sweep-${STAMP}.txt"
"${NET_DIR}/mtu-probe.sh" --netns "$NS_A" --host "$C_OVERLAY" --sweep --sweep-max 2000 | tee -a "${OUT_DIR}/sweep-${STAMP}.txt"

# ---- 2. sustained streaming load -------------------------------------------
echo
echo "=============== 2. sustained streaming load ============================"
run_load() {
  local label="$1" ns="$2" host="$3" duration="$4" out="$5"
  if "${NET_DIR}/inference-loadtest.sh" --url "http://${host}:${PORT}" --netns "$ns" \
    --duration "$duration" --concurrency 2 --prompt-bytes 65536 --out "$out"; then
    record "LOAD ${label}: PASS (0 dropped)"
  else
    record "LOAD ${label}: dropped connections"
    if [ "$label" = "direct" ]; then
      fail_count=$((fail_count + 1))
    fi
  fi
}
echo "--- direct path, ${DIRECT_DURATION}s ---"
run_load "direct" "$NS_A" "$C_UNDERLAY" "$DIRECT_DURATION" "${OUT_DIR}/loadtest-direct-${STAMP}.json"
echo "--- overlay path, ${OVERLAY_DURATION}s ---"
run_load "overlay" "$NS_A" "$C_OVERLAY" "$OVERLAY_DURATION" "${OUT_DIR}/loadtest-overlay-${STAMP}.json"

# ---- 3. black-hole overlay --------------------------------------------------
echo
echo "=============== 3. overlay PMTU black hole ============================="
# Agent believes the path MTU is ${UNDERLAY_MTU}; the tunnel hop can only carry
# 1450 (the 50-byte encap cost) and its ICMP frag-needed is filtered, so
# oversized prompt uploads vanish. This is the failure the direct route exists to
# avoid. Endpoints stay at 1500 — as guests do — so MSS negotiation cannot hide
# the mismatch.
ip link add bh-a1 netns "$NS_A" type veth peer name bh-h1 netns "$NS_H"
ip link add bh-h2 netns "$NS_H" type veth peer name bh-c1 netns "$NS_C"
ip -n "$NS_A" link set bh-a1 up mtu "$UNDERLAY_MTU"
ip -n "$NS_H" link set bh-h1 up mtu "$UNDERLAY_MTU"
ip -n "$NS_H" link set bh-h2 up mtu 1450
ip -n "$NS_C" link set bh-c1 up mtu "$UNDERLAY_MTU"
ip -n "$NS_A" addr add "${BH_A}/24" dev bh-a1
ip -n "$NS_H" addr add "${BH_H1}/24" dev bh-h1
ip -n "$NS_H" addr add "${BH_H2}/24" dev bh-h2
ip -n "$NS_C" addr add "${BH_C}/24" dev bh-c1
ip netns exec "$NS_H" sysctl -qw net.ipv4.ip_forward=1
ip -n "$NS_A" route add "${BH_C}/32" via "$BH_H1"
# Return path so the 1450-byte case can succeed; only the oversized case black-holes.
ip -n "$NS_C" route add "10.31.0.0/24" via "$BH_H2"
# Emulate a network that filters ICMP frag-needed (the black hole).
ip netns exec "$NS_A" iptables -A INPUT -p icmp --icmp-type fragmentation-needed -j DROP

probe "blackhole overlay @ ${UNDERLAY_MTU}" "$NS_A" "$BH_C" "$UNDERLAY_MTU" fail
probe "blackhole overlay @ 1450" "$NS_A" "$BH_C" 1450 pass
echo "--- black-hole streaming load, ${BLACKHOLE_DURATION}s (drops expected) ---"
if "${NET_DIR}/inference-loadtest.sh" --url "http://${BH_C}:${PORT}" --netns "$NS_A" \
  --duration "$BLACKHOLE_DURATION" --concurrency 2 --prompt-bytes 65536 \
  --request-timeout 8 --out "${OUT_DIR}/loadtest-blackhole-${STAMP}.json"; then
  record "LOAD blackhole: unexpectedly clean"
else
  record "LOAD blackhole: dropped connections (expected — demonstrates the failure mode)"
fi
# direct path stays clean under the same conditions
echo "--- direct path control, ${BLACKHOLE_DURATION}s ---"
run_load "direct-control" "$NS_A" "$C_UNDERLAY" "$BLACKHOLE_DURATION" "${OUT_DIR}/loadtest-direct-control-${STAMP}.json"

# ---- summary ----------------------------------------------------------------
echo
echo "=============== summary ================================================"
cat "/tmp/tnlab-summary.$$" 2>/dev/null || true
rm -f "/tmp/tnlab-summary.$$"
echo
echo "evidence written under: ${OUT_DIR}"
echo "run log: ${RUN_LOG}"
if [ "$fail_count" -eq 0 ]; then
  echo "LAB RESULT: PASS"
  exit 0
fi
echo "LAB RESULT: FAIL (${fail_count} unexpected results)"
exit 1
