#!/usr/bin/env bash
# =============================================================================
# scenario_a_oom.sh — Scenario A chaos: sustained memory pressure in a target VM.
#
# Runs `stress-ng --vm` INSIDE the target (an inference VM, reached via LXD exec
# or ssh/gcloud) to push memory utilisation above a configurable trigger
# threshold and HOLD it there for --hold-secs. The point is to leave the AI
# operator / telemetry a 20-40 s window to remediate BEFORE the kernel
# OOM-killer fires. A control-side monitor samples /proc/meminfo and
# /proc/pressure/memory every --sample-every seconds, records the exact
# threshold-crossing timestamp in the chaos log, and aborts the injection if
# utilisation reaches the safety ceiling (so the target is never OOM-killed by
# a runaway allocation).
#
# stress-ng is also started with a hard --timeout, so even a hard tool failure
# cannot leave the target under memory pressure.
#
# Usage: scenario_a_oom.sh [OPTIONS]
#   --target SPEC        local | lxd:<instance> | ssh:<[user@]host> | gcloud:<inst>
#                        (default: local)
#   --target-pct N       total utilisation % the injection aims to reach (94)
#   --trigger-pct N      utilisation % logged as "threshold crossed" (90)
#   --safety-pct N       abort the injection at/above this % (97)
#   --hold-secs N        seconds to hold above the trigger (25)
#   --timeout N          hard cap on the whole injection, seconds (120)
#   --vm-workers N       stress-ng --vm workers (1)
#   --ramp-steps N       allocate the target in N staged chunks (3)
#   --ramp-interval N    seconds between ramp chunks (3)
#   --sample-every N     monitor poll interval, seconds (1)
#   --log-dir DIR        chaos log directory (./chaos-logs)
#   --no-fail-on-oom     do not fail the run if the OOM-killer fires
#   --dry-run            print the plan; inject nothing
#   -h, --help
#
# Exit: 0 injected+verified, 2 usage, 3 OOM-killer fired during injection,
#       4 signature not produced, 5 target/prerequisite error.
# =============================================================================
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=chaos_lib.sh
. "$SCRIPT_DIR/chaos_lib.sh"

SCENARIO="scenario_a_oom"

TARGET="local"
TARGET_PCT="94"
TRIGGER_PCT="90"
SAFETY_PCT="97"
HOLD_SECS="25"
TIMEOUT_SECS="120"
VM_WORKERS="1"
RAMP_STEPS="3"
RAMP_INTERVAL="3"
SAMPLE_EVERY="1"
FAIL_ON_OOM="1"
DRY_RUN="0"
LOG_DIR="${CHAOS_LOG_DIR:-./chaos-logs}"

usage() {
  awk 'NR==1{next} /^# =+$/ { if (seen) exit; seen=1; next } seen { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

while [ $# -gt 0 ]; do
  case $1 in
    --target)          TARGET=${2:?--target needs a value}; shift 2 ;;
    --target-pct)      TARGET_PCT=${2:?--target-pct needs a value}; shift 2 ;;
    --trigger-pct)     TRIGGER_PCT=${2:?--trigger-pct needs a value}; shift 2 ;;
    --safety-pct)      SAFETY_PCT=${2:?--safety-pct needs a value}; shift 2 ;;
    --hold-secs)       HOLD_SECS=${2:?--hold-secs needs a value}; shift 2 ;;
    --timeout)         TIMEOUT_SECS=${2:?--timeout needs a value}; shift 2 ;;
    --vm-workers)      VM_WORKERS=${2:?--vm-workers needs a value}; shift 2 ;;
    --ramp-steps)      RAMP_STEPS=${2:?--ramp-steps needs a value}; shift 2 ;;
    --ramp-interval)   RAMP_INTERVAL=${2:?--ramp-interval needs a value}; shift 2 ;;
    --sample-every)    SAMPLE_EVERY=${2:?--sample-every needs a value}; shift 2 ;;
    --log-dir)         LOG_DIR=${2:?--log-dir needs a value}; shift 2 ;;
    --no-fail-on-oom)  FAIL_ON_OOM="0"; shift ;;
    --dry-run)         DRY_RUN="1"; shift ;;
    -h|--help)         usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

chaos_log_init "$SCENARIO" "$LOG_DIR"
chaos_target_init "$TARGET"
chaos_rc=0
trap 'chaos_rc=$?; chaos_run_cleanup; exit "$chaos_rc"' EXIT
trap 'chaos_rc=$?; chaos_run_cleanup; exit 130' INT
trap 'chaos_rc=$?; chaos_run_cleanup; exit 143' TERM

chaos_event run_start \
  target="$TARGET" target_pct:="$TARGET_PCT" trigger_pct:="$TRIGGER_PCT" \
  safety_pct:="$SAFETY_PCT" hold_secs:="$HOLD_SECS" timeout_s:="$TIMEOUT_SECS" \
  vm_workers:="$VM_WORKERS" ramp_steps:="$RAMP_STEPS" sample_every:="$SAMPLE_EVERY" \
  dry_run:="$DRY_RUN"

# --- prerequisites ---------------------------------------------------------
if [ "$DRY_RUN" != "1" ]; then
  if ! chaos_t_exec 'test -r /proc/meminfo' >/dev/null 2>&1; then
    chaos_fatal "target '$TARGET' has no readable /proc/meminfo"
  fi
  if ! chaos_t_has stress-ng; then
    chaos_fatal "stress-ng is not installed on target '$TARGET'" \
      "hint=apt-get install -y stress-ng (or bake it into the node image)"
  fi
fi

STRESS_PIDS=()

stop_stress() {
  local pid
  for pid in ${STRESS_PIDS[@]+"${STRESS_PIDS[@]}"}; do
    [ -n "$pid" ] || continue
    chaos_t_root "kill -TERM $pid 2>/dev/null; sleep 0.3; kill -KILL $pid 2>/dev/null; true" >/dev/null 2>&1 || true
  done
  STRESS_PIDS=()
}
chaos_add_cleanup stop_stress

# --- baseline --------------------------------------------------------------
base_line=$(chaos_t_exec "cat /proc/meminfo; echo ---PSI---; cat /proc/pressure/memory 2>/dev/null || true" 2>/dev/null || true)
read -r base_used base_avail base_free memtotal avail free swaptotal base_psi <<< "$(printf '%s\n' "$base_line" | chaos_parse_meminfo)"

chaos_event baseline \
  used_pct:="$base_used" avail_pct:="$base_avail" free_pct:="$base_free" \
  mem_total_kb:="$memtotal" mem_available_kb:="$avail" mem_free_kb:="$free" \
  swap_total_kb:="$swaptotal" psi_some:="$base_psi"

if [ "$memtotal" -le 0 ]; then
  chaos_fatal "could not read MemTotal from target '$TARGET'"
fi

# bytes to allocate = (target_pct% of RAM) - currently used bytes
alloc_bytes=$(awk -v t="$memtotal" -v a="$avail" -v p="$TARGET_PCT" \
  'BEGIN { b = (p/100.0*t - (t-a))*1024; if (b < 0) b = 0; printf "%d", b }')

if [ "$RAMP_STEPS" -lt 1 ]; then RAMP_STEPS=1; fi
chunk_bytes=$(( alloc_bytes / RAMP_STEPS ))
if [ "$alloc_bytes" -eq 0 ]; then
  chaos_warn "target already at/above ${TARGET_PCT}% utilisation; injection will hold only"
fi

chaos_event plan \
  alloc_bytes:="$alloc_bytes" chunk_bytes:="$chunk_bytes" ramp_steps:="$RAMP_STEPS" \
  "stress_cmd=stress-ng --vm ${VM_WORKERS} --vm-bytes <chunk>B --vm-keep --timeout ${TIMEOUT_SECS}s"

if [ "$DRY_RUN" = "1" ]; then
  chaos_event dry_run_complete "would inject ~${alloc_bytes} bytes over ${RAMP_STEPS} chunk(s) into $TARGET"
  chaos_summary '"dry_run":true'
  exit 0
fi

# --- inject ----------------------------------------------------------------
start_chunk() {
  local idx=$1 bytes=$2
  local logf="/tmp/${CHAOS_RUN_ID}-chunk${idx}.log"
  local pid
  pid=$(chaos_t_exec "nohup stress-ng --vm ${VM_WORKERS} --vm-bytes ${bytes}B --vm-keep --timeout ${TIMEOUT_SECS}s --quiet > ${logf} 2>&1 </dev/null & echo \$!" 2>/dev/null || true)
  pid=${pid//[^0-9]/}
  if [ -z "$pid" ]; then
    chaos_warn "chunk $idx: could not capture stress-ng pid"
    return 0
  fi
  STRESS_PIDS+=("$pid")
  chaos_event stress_start chunk:="$idx" pid:="$pid" bytes:="$bytes" log="$logf"
}

stress_alive() {
  local pid n=0
  for pid in ${STRESS_PIDS[@]+"${STRESS_PIDS[@]}"}; do
    [ -n "$pid" ] || continue
    if chaos_t_exec "kill -0 $pid 2>/dev/null && echo up || echo down" 2>/dev/null | grep -q up; then
      n=$(( n + 1 ))
    fi
  done
  [ "$n" -gt 0 ]
}

inject_start_iso=$(chaos_now_iso)
inject_start_epoch=$(chaos_now_epoch)
chaos_event inject_start "ts_inject=$inject_start_iso" alloc_bytes:="$alloc_bytes"

i=0
while [ "$i" -lt "$RAMP_STEPS" ]; do
  start_chunk "$i" "$chunk_bytes"
  i=$(( i + 1 ))
  if [ "$i" -lt "$RAMP_STEPS" ]; then sleep "$RAMP_INTERVAL"; fi
done

# --- monitor ---------------------------------------------------------------
crossed=0
safety_hit=0
peak_used="$base_used"
peak_psi="$base_psi"
trigger_ts=""
trigger_epoch=""
above_start_epoch=""
above_end_epoch=""
prev_ts=""
prev_used="$base_used"
stop_reason=""
deadline=$(( $(date -u +%s) + TIMEOUT_SECS ))

while :; do
  out=$(chaos_t_exec "cat /proc/meminfo; echo ---PSI---; cat /proc/pressure/memory 2>/dev/null || true" 2>/dev/null || true)
  s_ts=$(chaos_now_iso)
  s_epoch=$(chaos_now_epoch)
  read -r used_pct avail_pct free_pct total_kb avail_kb free_kb swaptotal_kb psi <<< "$(printf '%s\n' "$out" | chaos_parse_meminfo)"
  elapsed=$(awk -v a="$s_epoch" -v b="$inject_start_epoch" 'BEGIN{printf "%.1f", a-b}')

  chaos_event sample "ts_sample=$s_ts" used_pct:="$used_pct" avail_pct:="$avail_pct" \
    free_pct:="$free_pct" mem_total_kb:="$total_kb" mem_available_kb:="$avail_kb" \
    mem_free_kb:="$free_kb" swap_total_kb:="$swaptotal_kb" psi_some:="${psi:-0}" elapsed_s:="$elapsed"

  peak_used=$(awk -v p="$peak_used" -v u="$used_pct" 'BEGIN{print (u>p)?u:p}')
  peak_psi=$(awk -v p="$peak_psi" -v u="${psi:-0}" 'BEGIN{print (u>p)?u:p}')

  if [ "$crossed" = "0" ] && \
     [ "$(awk -v u="$used_pct" -v t="$TRIGGER_PCT" 'BEGIN{print (u>=t)?1:0}')" = "1" ]; then
    crossed=1
    trigger_ts="$s_ts"
    trigger_epoch="$s_epoch"
    above_start_epoch="$s_epoch"
    chaos_event threshold_crossed "trigger_ts=$trigger_ts" used_pct:="$used_pct" \
      trigger_pct:="$TRIGGER_PCT" prev_used_pct:="$prev_used" prev_ts="$prev_ts"
  fi

  if [ "$crossed" = "1" ]; then
    above_end_epoch="$s_epoch"
    if [ "$(awk -v u="$used_pct" -v s="$SAFETY_PCT" 'BEGIN{print (u>=s)?1:0}')" = "1" ]; then
      safety_hit=1
      stop_reason="safety_pct_reached"
      chaos_event safety_abort "used_pct=$used_pct" safety_pct:="$SAFETY_PCT"
      break
    fi
    dwell=$(awk -v n="$s_epoch" -v t="$trigger_epoch" 'BEGIN{printf "%.1f", n-t}')
    if [ "$(awk -v d="$dwell" -v h="$HOLD_SECS" 'BEGIN{print (d>=h)?1:0}')" = "1" ]; then
      stop_reason="hold_complete"
      break
    fi
  fi

  if ! stress_alive; then
    stop_reason="stress_exited"
    chaos_event stress_gone
    break
  fi
  if [ "$(date -u +%s)" -ge "$deadline" ]; then
    stop_reason="timeout"
    break
  fi
  prev_ts="$s_ts"
  prev_used="$used_pct"
  sleep "$SAMPLE_EVERY"
done

chaos_event inject_stop "ts_stop=$(chaos_now_iso)" reason="$stop_reason" \
  crossed:="$crossed" safety_hit:="$safety_hit" peak_used_pct:="$peak_used" peak_psi:="$peak_psi"

# --- stop the injection and confirm recovery -------------------------------
stop_stress

dwell_secs=0
if [ "$crossed" = "1" ] && [ -n "$above_start_epoch" ] && [ -n "$above_end_epoch" ]; then
  dwell_secs=$(awk -v a="$above_start_epoch" -v b="$above_end_epoch" 'BEGIN{printf "%.1f", b-a}')
fi

recovered=0
rec_deadline=$(( $(date -u +%s) + 30 ))
while :; do
  out=$(chaos_t_exec "cat /proc/meminfo" 2>/dev/null || true)
  read -r used_pct avail_pct free_pct total_kb avail_kb free_kb swaptotal_kb psi <<< "$(printf '%s\n' "$out" | chaos_parse_meminfo)"
  if [ "$(awk -v u="$used_pct" -v b="$base_used" 'BEGIN{print (u<=b+5)?1:0}')" = "1" ]; then
    recovered=1
    break
  fi
  if [ "$(date -u +%s)" -ge "$rec_deadline" ]; then
    break
  fi
  sleep 1
done
chaos_event recovery "ts_recovery=$(chaos_now_iso)" recovered:="$recovered" used_pct:="$used_pct" baseline_used_pct:="$base_used"

# --- verify ----------------------------------------------------------------
oom_detected=0
klog=$(chaos_t_klog_since "$inject_start_iso" 2>/dev/null || true)
if printf '%s\n' "$klog" | grep -qiE 'Out of memory|oom-kill|Killed process|oom_reaper'; then
  oom_detected=1
  oom_line=$(printf '%s\n' "$klog" | grep -iE 'Out of memory|oom-kill|Killed process|oom_reaper' | tail -1)
  chaos_event oom_detected "line=$oom_line" "ts_inject=$inject_start_iso"
fi

min_dwell=5
dwell_ok=$(awk -v d="$dwell_secs" -v m="$min_dwell" 'BEGIN{print (d>=m)?1:0}')
verified=0
if [ "$crossed" = "1" ] && [ "$dwell_ok" = "1" ]; then verified=1; fi

if [ "$dwell_ok" != "1" ]; then
  chaos_event verify_failed "reason=dwell_too_short" dwell_secs:="$dwell_secs" min_required:="$min_dwell"
elif [ "$crossed" != "1" ]; then
  chaos_event verify_failed "reason=threshold_never_crossed"
else
  chaos_event verify_ok "dwell_secs=$dwell_secs" peak_used_pct:="$peak_used" peak_psi:="$peak_psi"
fi

summary_body=$(printf '"verified":%s,"trigger_ts":"%s","dwell_secs":%s,"peak_used_pct":%s,"peak_psi_some":%s,"oom_detected":%s,"recovered":%s,"stop_reason":"%s","alloc_bytes":%s,"hold_secs":%s,"trigger_pct":%s,"target":"%s"' \
  "$([ "$verified" = 1 ] && echo true || echo false)" \
  "$trigger_ts" "$dwell_secs" "$peak_used" "$peak_psi" \
  "$([ "$oom_detected" = 1 ] && echo true || echo false)" \
  "$([ "$recovered" = 1 ] && echo true || echo false)" \
  "$stop_reason" "$alloc_bytes" "$HOLD_SECS" "$TRIGGER_PCT" "$TARGET")
chaos_summary "$summary_body" >/dev/null

rc=0
if [ "$oom_detected" = "1" ] && [ "$FAIL_ON_OOM" = "1" ]; then
  chaos_event run_failed "reason=oom_killer_fired_during_injection"
  rc=3
elif [ "$verified" != "1" ]; then
  chaos_event run_failed "reason=signature_not_produced" dwell_secs:="$dwell_secs"
  rc=4
fi

chaos_event run_end exit_code:="$rc" trigger_ts="$trigger_ts" dwell_secs:="$dwell_secs" oom_detected:="$oom_detected"
exit "$rc"
