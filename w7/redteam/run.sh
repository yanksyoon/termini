#!/usr/bin/env bash
# run.sh — W7 red-team: adversarial cloud-init boot-latency + fan-network
# packet-loss test for the Termini demo (kanban t_8d243ee1).
#
#   bash w7/redteam/run.sh [OUT_DIR]
#
# Runs against the live emulated demo on this host (controller :8085, LXD nodes
# demo-node-a/b/c from alias agentic-os/w3) plus a *dedicated* controller on
# :8097 for the boot-fault scenarios, so the running controller is not disturbed.
#
# Emits: <OUT_DIR>/boot/*.json, <OUT_DIR>/net/*.json and a top-level summary.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$HERE/evidence}"
mkdir -p "$OUT/boot" "$OUT/net"

echo "### boot: baseline (N=5)"
python3 "$HERE/boot/boot_latency.py" --scenario baseline --runs 5 --budget 45 \
  --demo-timeout 30 --out "$OUT/boot/baseline"

echo "### boot: delayed registration/metadata service (30s, N=3)"
python3 "$HERE/boot/boot_latency.py" --scenario delay --runs 3 --delay-s 30 --budget 45 \
  --demo-timeout 30 --out "$OUT/boot/delay"

echo "### boot: connection-refused registration (N=2)"
python3 "$HERE/boot/boot_latency.py" --scenario refuse --runs 2 --budget 20 \
  --demo-timeout 30 --out "$OUT/boot/refuse"

echo "### boot: unresolvable controller hostname (DNS, N=1)"
python3 "$HERE/boot/boot_latency.py" --scenario dnsfail --runs 1 --budget 20 \
  --demo-timeout 30 --out "$OUT/boot/dnsfail"

echo "### boot: consumed enrollment token (N=1)"
python3 "$HERE/boot/boot_latency.py" --scenario badtoken --runs 1 --budget 20 \
  --demo-timeout 30 --out "$OUT/boot/badtoken"

echo "### boot: legacy apt-at-boot cloud-init (break-the-demo repro)"
bash "$HERE/boot/legacy_apt_repro.sh" "$OUT/boot/legacy-apt" || true

for s in baseline latency loss kill; do
  echo "### net: $s"
  python3 "$HERE/net/net_chaos.py" --scenario "$s" --out "$OUT/net/$s"
done

echo "### done -> $OUT"
