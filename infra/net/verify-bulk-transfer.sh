#!/usr/bin/env bash
# verify-bulk-transfer.sh — N-run acceptance for infra/net/bulk-transfer.py.
#
# Runs the resilient chunked transfer between two live nodes under `tc netem`
# packet loss (default 30 %) and on the clean path, and writes a JSON summary.
# Exits non-zero if any loss run fails to complete inside the deadline, or the
# clean-path baseline regresses beyond --baseline-max.
#
#   infra/net/verify-bulk-transfer.sh \
#       --node-a rt-net-a --node-c rt-net-c --loss 30% --runs 5 --mb 64
#
# Requirements: lxc, the nodes reachable from this host, python3 on both nodes.
# Every tc qdisc is removed on exit.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE_A="${NODE_A:-rt-net-a}"
NODE_C="${NODE_C:-rt-net-c}"
LOSS="${LOSS:-30%}"
RUNS="${RUNS:-5}"
BASE_RUNS="${BASE_RUNS:-3}"
MB="${MB:-64}"
CHUNK="${CHUNK:-262144}"
STREAMS="${STREAMS:-8}"
DEADLINE="${DEADLINE:-30}"
RETRIES="${RETRIES:-8}"
CONN_TIMEOUT="${CONN_TIMEOUT:-3}"
PORT="${PORT:-9110}"
BASELINE_MAX="${BASELINE_MAX:-10.0}"
TRANSPORT="${TRANSPORT:-udp}"
PAYLOAD="${PAYLOAD:-1360}"
RATE="${RATE:-800}"

while [ $# -gt 0 ]; do
  case "$1" in
    --node-a) NODE_A="$2"; shift 2;;
    --node-c) NODE_C="$2"; shift 2;;
    --loss) LOSS="$2"; shift 2;;
    --runs) RUNS="$2"; shift 2;;
    --mb) MB="$2"; shift 2;;
    --out) OUT="$2"; shift 2;;
    --streams) STREAMS="$2"; shift 2;;
    --chunk) CHUNK="$2"; shift 2;;
    --deadline) DEADLINE="$2"; shift 2;;
    --transport) TRANSPORT="$2"; shift 2;;
    --payload) PAYLOAD="$2"; shift 2;;
    --rate-mbps) RATE="$2"; shift 2;;
    --baseline-max) BASELINE_MAX="$2"; shift 2;;
    *) echo "unknown arg $1" >&2; exit 2;;
  esac
done
OUT="${OUT:-$HERE/evidence/bulk-$(date -u +%Y%m%dT%H%M%SZ)}"
SZ=$((MB * 1024 * 1024))
mkdir -p "$OUT"

A_PY=/root/bulk-transfer.py
C_PY=/root/bulk-transfer.py
C_RECV=/root/bulk-recv.bin

cleanup() {
  lxc exec "$NODE_A" -- tc qdisc del dev eth0 root >/dev/null 2>&1 || true
  lxc exec "$NODE_C" -- pkill -f 'bulk-transfer.py server' >/dev/null 2>&1 || true
}
trap cleanup EXIT

CIP="$(lxc list "$NODE_C" --format csv -c 4 | tr ', ' '\n\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -1)"
[ -n "$CIP" ] || { echo "cannot resolve $NODE_C ip" >&2; exit 2; }
echo "### node A=$NODE_A  node C=$NODE_C ($CIP)  size=$SZ loss=$LOSS runs=$RUNS"

for n in "$NODE_A" "$NODE_C"; do
  lxc file push "$HERE/bulk-transfer.py" "$n/root/bulk-transfer.py" >/dev/null 2>&1 \
    || { echo "push to $n failed" >&2; exit 2; }
done

run_case() { # label i loss
  local label="$1" i="$2" loss="$3"
  lxc exec "$NODE_A" -- tc qdisc del dev eth0 root >/dev/null 2>&1 || true
  lxc exec "$NODE_C" -- pkill -f 'bulk-transfer.py server' >/dev/null 2>&1 || true
  lxc exec "$NODE_C" -- rm -f "$C_RECV" "$C_RECV.btstate" >/dev/null 2>&1 || true
  if [ -n "$loss" ]; then
    lxc exec "$NODE_A" -- tc qdisc add dev eth0 root netem loss "$loss" >/dev/null 2>&1 \
      || { echo "netem add failed" >&2; return 3; }
  fi
  local srv_opts="--transport $TRANSPORT --payload $PAYLOAD --rcvbuf 8388608"
  local cli_opts="--transport $TRANSPORT --payload $PAYLOAD --rate-mbps $RATE"
  local probe="ss -ltn"
  [ "$TRANSPORT" = udp ] && probe="ss -lun"
  lxc exec "$NODE_C" -- sh -c "setsid nohup python3 $C_PY server --port $PORT --size $SZ \
      --out $C_RECV --seconds $((DEADLINE + 60)) $srv_opts >/root/bt-srv.log 2>&1 < /dev/null &" >/dev/null 2>&1
  for _ in $(seq 1 25); do
    lxc exec "$NODE_C" -- sh -c "$probe | grep -q ':$PORT '" >/dev/null 2>&1 && break
    sleep 0.3
  done
  lxc exec "$NODE_A" -- python3 "$A_PY" client --host "$CIP" --port "$PORT" \
      --size "$SZ" $cli_opts --chunk "$CHUNK" --streams "$STREAMS" \
      --conn-timeout "$CONN_TIMEOUT" --retries "$RETRIES" --deadline "$DEADLINE" \
      --verify sha256 --out "/root/bt-cli.json" >"$OUT/client-${label}-${i}.log" 2>&1
  local rc=$?
  lxc exec "$NODE_A" -- cat /root/bt-cli.json >"$OUT/client-${label}-${i}.json" 2>/dev/null
  lxc exec "$NODE_C" -- pkill -f 'bulk-transfer.py server' >/dev/null 2>&1 || true
  lxc exec "$NODE_A" -- tc qdisc del dev eth0 root >/dev/null 2>&1 || true
  echo "  $label run $i: rc=$rc  $(sed -n '1p' "$OUT/client-${label}-${i}.log" | cut -c1-160)"
}

echo "### baseline (clean path)"
for i in $(seq 1 "$BASE_RUNS"); do run_case baseline "$i" ""; done
echo "### loss $LOSS"
for i in $(seq 1 "$RUNS"); do run_case loss "$i" "$LOSS"; done

python3 - "$OUT" "$LOSS" "$MB" "$DEADLINE" "$BASELINE_MAX" <<'PY'
import glob, json, os, sys
out, loss, mb, deadline, bmax = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5])
def load(tag):
    rows = []
    for p in sorted(glob.glob(os.path.join(out, f"client-{tag}-*.json"))):
        try:
            rows.append(json.load(open(p)))
        except Exception:
            rows.append({"completed": False, "error": "no json"})
    return rows
def summarize(rows):
    ok = [r for r in rows if r.get("completed")]
    times = sorted(r["elapsed_s"] for r in ok)
    p95 = times[min(len(times)-1, int(0.95*len(times)))] if times else None
    return {
        "runs": len(rows), "completed": len(ok),
        "elapsed_s": [r.get("elapsed_s") for r in rows],
        "mean_s": round(sum(times)/len(times), 3) if times else None,
        "max_s": times[-1] if times else None,
        "p95_s": p95,
        "rounds": [r.get("rounds") for r in rows],
        "wire_bytes": [r.get("wire_bytes") for r in rows],
        "retransmits": [r.get("server", {}).get("retransmits") for r in rows],
        "chunk_retries": [r.get("chunk_retries") for r in rows],
        "sha256_match": [r.get("sha256_match") for r in rows],
    }
base, lossr = load("baseline"), load("loss")
summary = {
    "target_mb": mb, "loss": loss, "deadline_s": deadline,
    "baseline": summarize(base), "under_loss": summarize(lossr),
}
fail = []
if base and base[0].get("max_s", 0) is None:
    pass
if summary["baseline"]["completed"] < summary["baseline"]["runs"]:
    fail.append("baseline did not complete for every run")
if summary["baseline"]["max_s"] and summary["baseline"]["max_s"] > bmax:
    fail.append(f"baseline regression: max {summary['baseline']['max_s']}s > {bmax}s")
if summary["under_loss"]["completed"] < summary["under_loss"]["runs"]:
    fail.append("loss run(s) failed to complete")
for r in lossr:
    if r.get("completed") and not r.get("sha256_match", True):
        fail.append("sha256 mismatch under loss")
summary["verdict"] = "PASS" if not fail else "FAIL"
summary["failures"] = fail
json.dump(summary, open(os.path.join(out, "summary.json"), "w"), indent=2)
print(json.dumps(summary, indent=2))
sys.exit(0 if not fail else 1)
PY
rc=$?
echo "### evidence: $OUT"
exit $rc
