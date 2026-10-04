#!/usr/bin/env bash
# finish.sh — run the remaining red-team scenarios (boot re-runs with the fixed
# fault proxy + badtoken consumption, the legacy-apt repro, and the full
# fan-network chaos suite) against the evidence root, then drop a DONE marker.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$HERE/evidence}"
mkdir -p "$OUT/boot" "$OUT/net"
cd "$HERE/../.."   # repo root
rm -f "$OUT/finish.done"

echo "### boot: delayed registration (8s, N=3)"
python3 w7/redteam/boot/boot_latency.py --scenario delay --runs 3 --delay-s 8 --budget 50 --demo-timeout 30 --out "$OUT/boot/delay"
echo "### boot: connection-refused (N=2)"
python3 w7/redteam/boot/boot_latency.py --scenario refuse --runs 2 --budget 20 --demo-timeout 30 --out "$OUT/boot/refuse"
echo "### boot: DNS failure (N=1)"
python3 w7/redteam/boot/boot_latency.py --scenario dnsfail --runs 1 --budget 20 --demo-timeout 30 --out "$OUT/boot/dnsfail"
echo "### boot: consumed token (N=1 + warmup)"
python3 w7/redteam/boot/boot_latency.py --scenario badtoken --runs 1 --budget 20 --demo-timeout 30 --out "$OUT/boot/badtoken"
echo "### boot: legacy apt-at-boot repro"
bash w7/redteam/boot/legacy_apt_repro.sh "$OUT/boot/legacy-apt" 90 || true

echo "### net: provision dedicated 3-node underlay"
bash w7/redteam/net/env_up.sh "$OUT/net/env" || { echo "ENV_UP_FAILED"; echo "DONE rc=1" > "$OUT/finish.done"; exit 1; }
for s in baseline latency loss kill; do
  echo "### net: $s"
  python3 w7/redteam/net/net_chaos.py --scenario "$s" --env "$OUT/net/env/env.json" --out "$OUT/net/$s"
done

echo "DONE rc=0" > "$OUT/finish.done"
echo "### ALL DONE"
