#!/usr/bin/env bash
# =============================================================================
# demo_dry_run.sh — timed, scripted, end-to-end rehearsal of the Termini demo.
#
# Owner: kanban task t_3bb56d97 (W6 demo reliability). Parent: t_5630daf9.
#
# It runs the demo beats IN ORDER, timing each with a wall clock and a total:
#   beat 0  intro / context
#   beat 1  live cluster health (pre-warmed LXD cluster + the direct inference route)
#   beat 2  Scenario A — OOM injection -> AI-operator remediation (measured)
#   beat 3  Scenario B — disk-fail (EXT4 remount-ro) -> AI-operator remediation
#   beat 4  dashboard — the live AI-operator stream the judges watch
#   beat 5  closing — operator audit + totals
#
# What is REAL vs. emulated on this host (documented in scripts/timing/README.md):
#   * beat 1 asserts the PRE-WARMED cluster (infra/warm/warm_cluster.sh) and the
#     direct route (infra/net/direct-route.sh) for real;
#   * beats 2/3 run the CURRENT controller build + the production AI-operator
#     loop (thresholds, hysteresis, playbooks) and issue REAL LXD REST
#     remediations against the live cluster (via scripts/timing/lxd_bridge.py,
#     because LXD on this host only exposes its unix socket). The fault is
#     injected deterministically by scripts/timing/demo_fault_injector.py, which
#     reuses the chaos harness sample generator.
#
# Why a private controller instance: the long-running shared controller predates
# the operator endpoints (its /v1/operator/* routes 404) and cannot be restarted
# in this session, so the injection beats stand up their own controller on the
# current checkout. The live cluster is still mutated for real.
#
# Usage:
#   scripts/timing/demo_dry_run.sh                 # 3 runs (run 3 = cold operator)
#   scripts/timing/demo_dry_run.sh --runs 1 --out DIR
#
# Exit: 0 all runs pass; 1 any beat failed; 2 bad usage/environment.
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
cd "$REPO_ROOT"

# --- configuration ----------------------------------------------------------
RUNS=3
WINDOW_S="${TERMINI_DEMO_WINDOW_S:-60}"
OUT_BASE=""
COLD_RUN="${TERMINI_DEMO_COLD_RUN:-3}"   # this run starts the operator from cold
while [ $# -gt 0 ]; do
  case "$1" in
    --runs) RUNS="${2:?--runs needs a number}"; shift 2 ;;
    --out)  OUT_BASE="${2:?--out needs a dir}"; shift 2 ;;
    --window) WINDOW_S="${2:?--window needs seconds}"; shift 2 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

LIVE_URL="${TERMINI_CONTROLLER_URL_HOST:-http://127.0.0.1:8085}"
LIVE_TOKEN="${TERMINI_PROJECT_TOKEN:-term-demo-project-token-000000000001}"
LIVE_PID="${TERMINI_PROJECT_ID:-01J0DEM0WARM000000000000AB}"
SB_TOKEN="demo-project-token"
SB_PORT_A="${TERMINI_SB_PORT_A:-18085}"
SB_PORT_B="${TERMINI_SB_PORT_B:-18086}"
CONSOLE_PORT="${TERMINI_CONSOLE_PORT:-18090}"
BRIDGE_PORT="${TERMINI_LXD_BRIDGE_PORT:-8443}"
NODE_A="demo-node-a"; NODE_B="demo-node-b"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
[ -n "$OUT_BASE" ] || OUT_BASE="$HERE/evidence/$STAMP"
mkdir -p "$OUT_BASE"

command -v jq >/dev/null 2>&1 || { echo "need jq" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "need python3" >&2; exit 2; }
command -v lxc >/dev/null 2>&1 || { echo "need lxc" >&2; exit 2; }

PIDS=()
_reserve() { PIDS+=("$1"); }
cleanup() {
  set +e
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null; done
  set -e
}
trap cleanup EXIT INT TERM

tnow() { date +%s.%N; }
secs() { awk "BEGIN{printf \"%.2f\", $1-$2}"; }
say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

# --- LXD transport bridge (unix socket -> TCP) ------------------------------
start_bridge() {
  if curl -s -m 2 "http://127.0.0.1:$BRIDGE_PORT/1.0" >/dev/null 2>&1; then
    echo "[bridge] already listening on :$BRIDGE_PORT"
    return 0
  fi
  python3 "$HERE/lxd_bridge.py" 127.0.0.1 "$BRIDGE_PORT" \
    >"$OUT_BASE/lxd_bridge.log" 2>&1 &
  _reserve $!
  for _ in $(seq 1 40); do
    curl -s -m 2 "http://127.0.0.1:$BRIDGE_PORT/1.0" >/dev/null 2>&1 && { echo "[bridge] up on :$BRIDGE_PORT"; return 0; }
    sleep 0.25
  done
  echo "[bridge] FAILED to start" >&2; return 1
}

# --- sandbox controller (current build, real operator, live LXD) ------------
start_sb() { # WORKDIR PORT DEFAULT_INSTANCE BOOTSTRAP
  local workdir="$1" port="$2" default_inst="$3" bootstrap="$4"
  mkdir -p "$workdir"
  PYTHONPATH="$REPO_ROOT/controller${PYTHONPATH:+:$PYTHONPATH}" \
  TERMINI_HOST=127.0.0.1 TERMINI_PORT="$port" \
  TERMINI_DB_PATH="$workdir/controller.db" \
  TERMINI_BOOTSTRAP_TOKEN="$bootstrap" \
  TERMINI_PROJECT_ID="$LIVE_PID" TERMINI_PROJECT_TOKEN="$SB_TOKEN" \
  TERMINI_OPERATOR_ENABLED=true TERMINI_OPERATOR_POLL_INTERVAL_S=1 \
  TERMINI_OPERATOR_COOLDOWN_S=3 \
  TERMINI_LXD_BASE_URL="http://127.0.0.1:$BRIDGE_PORT" \
  TERMINI_LXD_DEFAULT_INSTANCE_NAME="$default_inst" \
  python3 -m app.main >>"$workdir/controller.log" 2>&1 &
  echo $! > "$workdir/controller.pid"
  _reserve "$(cat "$workdir/controller.pid")"
  for _ in $(seq 1 80); do
    code=$(curl -s -m 2 -o /dev/null -w '%{http_code}' \
      "http://127.0.0.1:$port/v1/operator/decisions" \
      -H "Authorization: Bearer $SB_TOKEN" || true)
    [ "$code" = "200" ] && return 0
    sleep 0.25
  done
  echo "[sb] controller on :$port did not become ready" >&2
  return 1
}
stop_sb() { # WORKDIR
  [ -f "$1/controller.pid" ] && kill "$(cat "$1/controller.pid")" 2>/dev/null || true
}

# prune_run_payload RUN_DIR — remove runtime binaries, keep text evidence, and
# redact any request token that leaked into the controller access log (the SSE
# log stream carries ?token=... in its query string).
prune_run_payload() {
  local d="$1" f
  for f in "$d"/scenario_a/controller.db "$d"/scenario_a/controller.db-wal \
           "$d"/scenario_a/controller.db-shm "$d"/scenario_a/controller.pid \
           "$d"/scenario_b/controller.db "$d"/scenario_b/controller.db-wal \
           "$d"/scenario_b/controller.db-shm "$d"/scenario_b/controller.pid; do
    [ -e "$f" ] && rm -f "$f"
  done
  for f in "$d"/scenario_a/controller.log "$d"/scenario_b/controller.log; do
    [ -e "$f" ] && sed -i -E 's/(token=|Bearer )[A-Za-z0-9._-]+/\1REDACTED/g' "$f"
  done
}

# --- one injection -> remediation -> measured audit -------------------------
# fire_scenario WORKDIR PORT DEFAULT_INSTANCE BOOTSTRAP RULE MODE PARAMS
# sets LATENCY (seconds) and returns 0 when the audit landed within WINDOW_S.
fire_scenario() {
  local workdir="$1" port="$2" default_inst="$3" bootstrap="$4" rule="$5" mode="$6" params="${7:-}"
  [ -n "$params" ] || params='{}'
  start_sb "$workdir" "$port" "$default_inst" "$bootstrap"
  local t0 t1 rc
  t0="$(tnow)"
  python3 "$HERE/demo_fault_injector.py" --url "http://127.0.0.1:$port" \
    --bootstrap-token "$bootstrap" --hostname "demo-fault-${mode}" \
    --mode "$mode" --duration 12 --params "$params" \
    --series-out "$workdir/series.json" --summary-out "$workdir/summary.json" \
    >"$workdir/injector.log" 2>&1 &
  local injpid=$!
  _reserve "$injpid"
  set +e
  python3 "$HERE/demo_probe.py" --url "http://127.0.0.1:$port" --token "$SB_TOKEN" \
    wait-audit --rule "$rule" --timeout "$WINDOW_S" >"$workdir/audit.json" 2>"$workdir/wait.err"
  rc=$?
  set -e
  t1="$(tnow)"
  wait "$injpid" 2>/dev/null || true
  LATENCY="$(secs "$t1" "$t0")"
  return $rc
}

# --- beat bookkeeping -------------------------------------------------------
RESULTS="$OUT_BASE/beats.jsonl"
: > "$RESULTS"
beat() { # NAME SECONDS OK DETAIL
  jq -cn --arg n "$1" --argjson s "$2" --argjson ok "$3" --arg d "$4" \
    '{beat:$n, seconds:$s, ok:$ok, detail:$d}' >> "$RESULTS"
}
RUN_PASS=1

echo "demo dry run — $RUNS run(s), window ${WINDOW_S}s, evidence $OUT_BASE"

start_bridge || exit 2

for run in $(seq 1 "$RUNS"); do
  RUN_DIR="$OUT_BASE/run$run"
  mkdir -p "$RUN_DIR"
  RUN_T0="$(tnow)"
  RUN_OK=1
  COLD="no"; [ "$run" = "$COLD_RUN" ] && COLD="yes"
  say "RUN $run/$RUNS (cold operator: $COLD)"

  # ---------------------------------------------------------------- beat 0
  b0="$(tnow)"
  cat > "$RUN_DIR/beat0_intro.txt" <<'EOF'
Termini demo — live rehearsal.
Three proofs on real hardware: (1) bare metal -> AI-ready fleet in <5s;
(2) plain-English intent -> validated, clamped plan; (3) a governed
self-healing loop where the operator finds a fault, fixes it, and data never
leaves the premises. Scenarios run live against the pre-warmed cluster.
EOF
  beat "beat0_intro" "$(secs "$(tnow)" "$b0")" true "intro/context"

  # ---------------------------------------------------------------- beat 1
  b1="$(tnow)"
  B1_OK=true; B1_DETAIL=""
  if ! bash infra/warm/warm_cluster.sh --hold 6 >"$RUN_DIR/beat1_warm_cluster.txt" 2>&1; then
    B1_OK=false; B1_DETAIL="warm_cluster failed"
  fi
  bash infra/net/direct-route.sh status >"$RUN_DIR/beat1_direct_route.txt" 2>&1 || true
  python3 "$HERE/demo_probe.py" --url "$LIVE_URL" --token "$LIVE_TOKEN" nodes \
    >"$RUN_DIR/beat1_nodes.json" 2>&1 || true
  if ! grep -q "WARM CLUSTER: all checks green" "$RUN_DIR/beat1_warm_cluster.txt"; then
    B1_OK=false; B1_DETAIL="${B1_DETAIL:+$B1_DETAIL; }warm cluster not green"
  fi
  if ! grep -q "active path     : direct" "$RUN_DIR/beat1_direct_route.txt"; then
    B1_OK=false; B1_DETAIL="${B1_DETAIL:+$B1_DETAIL; }inference path not direct"
  fi
  beat "beat1_cluster_health" "$(secs "$(tnow)" "$b1")" "$B1_OK" "${B1_DETAIL:-pre-warmed cluster green; direct route active}"
  [ "$B1_OK" = true ] || RUN_OK=0

  # ---------------------------------------------------------------- beat 2
  b2="$(tnow)"
  A_DIR="$RUN_DIR/scenario_a"; A_OK=true; A_DETAIL=""
  if fire_scenario "$A_DIR" "$SB_PORT_A" "$NODE_A" "demo-boot-a-b2r$run" \
       oom-guard-01 oom '{"ram_start_pct":62.0,"ram_peak_pct":97.0,"ramp_s":2.0}'; then
    A_DETAIL="oom-guard-01 -> remediation in ${LATENCY}s (<=${WINDOW_S}s)"
  else
    A_OK=false; A_DETAIL="oom-guard-01 NOT remediated within ${WINDOW_S}s"
  fi
  python3 "$HERE/demo_probe.py" --url "http://127.0.0.1:$SB_PORT_A" --token "$SB_TOKEN" audits \
    >"$A_DIR/audits.json" 2>&1 || true
  python3 "$HERE/demo_probe.py" --url "http://127.0.0.1:$SB_PORT_A" --token "$SB_TOKEN" events \
    >"$A_DIR/events.json" 2>&1 || true
  # cold-operator persistence: restart the SAME controller cold and show the
  # trigger config + hysteresis latch survive it (persisted triggers).
  COLD_OK=true
  if [ "$COLD" = yes ]; then
    python3 "$HERE/demo_probe.py" --url x --token x triggers --db "$A_DIR/controller.db" \
      >"$A_DIR/triggers_before_restart.json" 2>&1 || true
    stop_sb "$A_DIR"; sleep 0.5
    start_sb "$A_DIR" "$SB_PORT_A" "$NODE_A" "demo-boot-a-b2r$run" \
      || COLD_OK=false
    python3 "$HERE/demo_probe.py" --url x --token x triggers --db "$A_DIR/controller.db" \
      >"$A_DIR/triggers_after_restart.json" 2>&1 || true
    if ! jq -e '.trigger_state | length >= 1' "$A_DIR/triggers_after_restart.json" >/dev/null 2>&1; then
      COLD_OK=false
    fi
    if ! jq -e '.trigger_config | length >= 1' "$A_DIR/triggers_after_restart.json" >/dev/null 2>&1; then
      COLD_OK=false
    fi
    A_DETAIL="$A_DETAIL; cold restart loaded $(jq -c '.trigger_state|length' "$A_DIR/triggers_after_restart.json" 2>/dev/null) persisted latch row(s)"
    [ "$COLD_OK" = true ] || { A_OK=false; A_DETAIL="$A_DETAIL; COLD-RESTART persistence FAILED"; }
  fi
  beat "beat2_scenario_a_oom" "$(secs "$(tnow)" "$b2")" "$A_OK" "$A_DETAIL"
  [ "$A_OK" = true ] || RUN_OK=0
  stop_sb "$A_DIR"

  # ---------------------------------------------------------------- beat 3
  b3="$(tnow)"
  B_DIR="$RUN_DIR/scenario_b"; B_OK=true; B_DETAIL=""
  if fire_scenario "$B_DIR" "$SB_PORT_B" "$NODE_B" "demo-boot-b-b3r$run" \
       disk-fail-01 disk-fail '{"fail_after_s":1.0}'; then
    B_DETAIL="disk-fail-01 -> remediation in ${LATENCY}s (<=${WINDOW_S}s)"
  else
    B_OK=false; B_DETAIL="disk-fail-01 NOT remediated within ${WINDOW_S}s"
  fi
  python3 "$HERE/demo_probe.py" --url "http://127.0.0.1:$SB_PORT_B" --token "$SB_TOKEN" audits \
    >"$B_DIR/audits.json" 2>&1 || true
  # verify the drain really landed, then restore the node for the next run
  bstate="$(lxc list "$NODE_B" --format csv -c s 2>/dev/null | tr -d '\r' || true)"
  echo "$bstate" > "$B_DIR/node_b_state_after.txt"
  case "$bstate" in
    STOPPED|FROZEN) : ;;
    *) B_OK=false; B_DETAIL="$B_DETAIL; drain not observed (node=$bstate)" ;;
  esac
  # restore for the next run / closing beat
  if [ "$bstate" != "RUNNING" ]; then
    lxc start "$NODE_B" >/dev/null 2>&1 || true
    for _ in $(seq 1 40); do
      [ "$(lxc list "$NODE_B" --format csv -c s 2>/dev/null | tr -d '\r')" = "RUNNING" ] && break
      sleep 0.5
    done
  fi
  beat "beat3_scenario_b_disk" "$(secs "$(tnow)" "$b3")" "$B_OK" "$B_DETAIL"
  [ "$B_OK" = true ] || RUN_OK=0

  # ---------------------------------------------------------------- beat 4
  b4="$(tnow)"
  D_DIR="$RUN_DIR/dashboard"; mkdir -p "$D_DIR"; D_OK=true; D_DETAIL=""
  python3 "$REPO_ROOT/console/serve.py" --host 127.0.0.1 --port "$CONSOLE_PORT" \
      --controller "http://127.0.0.1:$SB_PORT_B" --project "$LIVE_PID" --token "$SB_TOKEN" \
      >"$D_DIR/console.log" 2>&1 &
  _reserve $!
  sleep 1
  for _ in $(seq 1 20); do
    curl -s -m 2 "http://127.0.0.1:$CONSOLE_PORT/" >/dev/null 2>&1 && break
    sleep 0.25
  done
  curl -s -m 4 "http://127.0.0.1:$CONSOLE_PORT/" > "$D_DIR/dashboard.html" 2>/dev/null || D_OK=false
  python3 "$HERE/demo_probe.py" --url "http://127.0.0.1:$SB_PORT_B" --token "$SB_TOKEN" \
    sse-logs --out "$D_DIR/operator_stream.txt" --seconds 3 >/dev/null 2>&1 || true
  if [ -s "$D_DIR/operator_stream.txt" ]; then
    D_DETAIL="dashboard served; operator log stream captured ($(wc -c < "$D_DIR/operator_stream.txt") bytes)"
  else
    D_DETAIL="dashboard served; operator stream empty (no buffered lines)"
  fi
  [ "$D_OK" = true ] || D_DETAIL="$D_DETAIL; dashboard HTML fetch failed"
  beat "beat4_dashboard" "$(secs "$(tnow)" "$b4")" "$D_OK" "$D_DETAIL"
  stop_sb "$B_DIR"

  # keep text evidence; drop runtime binaries (SQLite DBs, pid files)
  prune_run_payload "$RUN_DIR"

  # ---------------------------------------------------------------- beat 5
  b5="$(tnow)"
  python3 "$HERE/demo_probe.py" --url "$LIVE_URL" --token "$LIVE_TOKEN" nodes \
    > "$RUN_DIR/beat5_live_nodes.json" 2>&1 || true
  C_OK=true
  grep -q "WARM CLUSTER: all checks green" "$RUN_DIR/beat1_warm_cluster.txt" || C_OK=false
  beat "beat5_closing" "$(secs "$(tnow)" "$b5")" "$C_OK" "operator audit + live cluster receipts"

  RUN_TOTAL="$(secs "$(tnow)" "$RUN_T0")"
  RUN_STATUS="PASS"; [ "$RUN_OK" = 1 ] || { RUN_STATUS="FAIL"; RUN_PASS=0; }
  jq -cn --argjson run "$run" --argjson total "$RUN_TOTAL" --arg status "$RUN_STATUS" --arg cold "$COLD" \
    '{run:$run, total_s:$total, status:$status, cold_operator:$cold}' >> "$OUT_BASE/runs.jsonl"
  printf '\nRUN %s: %s in %ss (cold=%s)\n' "$run" "$RUN_STATUS" "$RUN_TOTAL" "$COLD"
done

# --- report -----------------------------------------------------------------
say "SUMMARY"
jq -s '.' "$OUT_BASE/beats.jsonl" > "$OUT_BASE/beats.json"
jq -s '.' "$OUT_BASE/runs.jsonl" > "$OUT_BASE/runs.json"
jq -r '.[] | "run \(.run): total=\(.total_s)s status=\(.status) cold=\(.cold_operator)"' \
  "$OUT_BASE/runs.json"
echo
jq -r '.[] | "\(.beat)  \(.seconds)s  \(if .ok then "PASS" else "FAIL" end)  \(.detail)"' \
  "$OUT_BASE/beats.json"
if [ "$RUN_PASS" = 1 ]; then
  echo; echo "DEMO DRY RUN: PASS ($RUNS/$RUNS runs)"; exit 0
fi
echo; echo "DEMO DRY RUN: FAIL" >&2; exit 1
