#!/usr/bin/env bash
# =============================================================================
# chaos_lib.sh — shared helpers for the Termini demo chaos scenarios.
#
# Sourced by scenario_a_oom.sh and scenario_b_disk.sh. It is NOT executable on
# its own. Provides:
#
#   * chaos logging: a human-readable .log plus a machine-readable .jsonl
#     "chaos log" of timestamped events (RFC3339 UTC, millisecond precision).
#     The JSONL timeline is what the telemetry/controller owner tunes trigger
#     thresholds against.
#   * a target transport abstraction so the same script can inject into a local
#     node, an LXD instance (VM/container), an ssh host, or a GCE instance:
#         --target local | lxd:<instance> | ssh:<[user@]host> | gcloud:<instance>
#   * inject / verify / cleanup helpers: a LIFO cleanup stack run from a trap,
#     dry-run gating, prerequisite checks, and kernel-log scraping.
#
# Conventions (match docs/contracts/node-telemetry.md): timestamps are UTC
# RFC3339, memory sizes are bytes, utilisation is percent.
# =============================================================================
# shellcheck shell=bash

export CHAOS_LIB_VERSION="1.0.0"

# ---------------------------------------------------------------------------
# clock
# ---------------------------------------------------------------------------
chaos_now_iso()   { date -u +%Y-%m-%dT%H:%M:%S.%3NZ; }
chaos_now_epoch() { date -u +%s.%3N; }

# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------
CHAOS_RUN_ID="${CHAOS_RUN_ID:-}"
CHAOS_SCENARIO="${CHAOS_SCENARIO:-unknown}"
CHAOS_LOG_DIR="${CHAOS_LOG_DIR:-}"
CHAOS_JSONL="${CHAOS_JSONL:-/dev/null}"
CHAOS_LOGFILE="${CHAOS_LOGFILE:-/dev/null}"
CHAOS_SUMMARY_FILE="${CHAOS_SUMMARY_FILE:-/dev/null}"
CHAOS_CLEANUP_FNS=()
CHAOS_CLEANUP_DONE=0

# chaos_json_escape STRING -> prints STRING escaped for a JSON string literal.
chaos_json_escape() {
  local s=$1
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\n'/\\n}
  s=${s//$'\r'/\\r}
  s=${s//$'\t'/\\t}
  printf '%s' "$s"
}

# chaos_log_init SCENARIO [LOG_DIR] — create the run id and log files.
chaos_log_init() {
  local scenario=$1
  local log_dir=${2:-${CHAOS_LOG_DIR:-./chaos-logs}}
  CHAOS_SCENARIO="$scenario"
  CHAOS_RUN_ID="${scenario}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  CHAOS_LOG_DIR="$log_dir"
  mkdir -p "$log_dir"
  CHAOS_JSONL="$log_dir/${CHAOS_RUN_ID}.jsonl"
  CHAOS_LOGFILE="$log_dir/${CHAOS_RUN_ID}.log"
  CHAOS_SUMMARY_FILE="$log_dir/${CHAOS_RUN_ID}.summary.json"
  : > "$CHAOS_JSONL"
  : > "$CHAOS_LOGFILE"
}

# chaos_event EVENT [KEY=VALUE | KEY:=RAW_JSON | "free text"]...
#   KEY=VALUE  -> JSON string value
#   KEY:=VALUE -> raw JSON value (numbers, booleans) — never shell-expanded
#   bare arg   -> emitted under "msg"
# Every event is appended to the JSONL chaos log and a human line to stderr+log.
chaos_event() {
  local event=$1; shift || true
  local ts epoch kv="" human=""
  ts=$(chaos_now_iso)
  epoch=$(chaos_now_epoch)
  local arg
  for arg in "$@"; do
    case $arg in
      *:=*) kv+=",\"$(chaos_json_escape "${arg%%:=*}")\":${arg#*:=}" ;;
      *=*)  kv+=",\"$(chaos_json_escape "${arg%%=*}")\":\"$(chaos_json_escape "${arg#*=}")\"" ;;
      *)    kv+=",\"msg\":\"$(chaos_json_escape "$arg")\"" ;;
    esac
    human+=" $arg"
  done
  if [ -n "$CHAOS_JSONL" ] && [ -w "$CHAOS_JSONL" ]; then
    printf '{"ts":"%s","epoch":%s,"run_id":"%s","scenario":"%s","event":"%s"%s}\n' \
      "$ts" "$epoch" "$CHAOS_RUN_ID" "$CHAOS_SCENARIO" "$event" "$kv" >> "$CHAOS_JSONL"
  fi
  printf '%s [%s] %s%s\n' "$ts" "$CHAOS_SCENARIO" "$event" "$human" >&2
  if [ -n "$CHAOS_LOGFILE" ] && [ -w "$CHAOS_LOGFILE" ]; then
    printf '%s [%s] %s%s\n' "$ts" "$CHAOS_SCENARIO" "$event" "$human" >> "$CHAOS_LOGFILE"
  fi
}

chaos_log()  { chaos_event log "$@"; }
chaos_warn() { chaos_event warn "$@"; }
chaos_fatal() {
  chaos_event fatal "$@"
  exit "${CHAOS_EXIT_CODE:-1}"
}

# chaos_summary JSON_BODY — write the final run summary (also a sidecar file).
chaos_summary() {
  local body=$1
  local ts
  ts=$(chaos_now_iso)
  local json
  json=$(printf '{"ts":"%s","run_id":"%s","scenario":"%s",%s}' \
    "$ts" "$CHAOS_RUN_ID" "$CHAOS_SCENARIO" "$body")
  if [ -n "$CHAOS_SUMMARY_FILE" ] && [ "$CHAOS_SUMMARY_FILE" != /dev/null ]; then
    printf '%s\n' "$json" > "$CHAOS_SUMMARY_FILE"
  fi
  printf '%s\n' "$json"
}

# ---------------------------------------------------------------------------
# cleanup stack (LIFO, idempotent, trap-safe)
# ---------------------------------------------------------------------------
chaos_add_cleanup() { CHAOS_CLEANUP_FNS+=("$1"); }

chaos_run_cleanup() {
  [ "${CHAOS_CLEANUP_DONE:-0}" = 1 ] && return 0
  CHAOS_CLEANUP_DONE=1
  local i fn
  for (( i=${#CHAOS_CLEANUP_FNS[@]}-1; i>=0; i-- )); do
    fn=${CHAOS_CLEANUP_FNS[$i]}
    chaos_event cleanup_step "fn=$fn"
    if "$fn"; then
      chaos_event cleanup_ok "fn=$fn"
    else
      chaos_event cleanup_failed "fn=$fn"
    fi
  done
}

chaos_enable_cleanup_trap() {
  chaos_rc=0
  trap 'chaos_rc=$?; chaos_run_cleanup; exit "$chaos_rc"' EXIT
  trap 'chaos_rc=$?; chaos_run_cleanup; exit 130' INT
  trap 'chaos_rc=$?; chaos_run_cleanup; exit 143' TERM
}

# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------
CHAOS_DRY_RUN="${CHAOS_DRY_RUN:-0}"

# chaos_plan CMD... — record (and print) an action that would mutate state.
chaos_plan() {
  chaos_event plan "$@"
}

# ---------------------------------------------------------------------------
# target transport
# ---------------------------------------------------------------------------
CHAOS_TARGET="${CHAOS_TARGET:-}"
CHAOS_TT="${CHAOS_TT:-}"

chaos_target_init() {
  local spec=$1
  CHAOS_TARGET="$spec"
  case $spec in
    local)    CHAOS_TT=local ;;
    lxd:*)    CHAOS_TT=lxd;     CHAOS_LXD_INSTANCE=${spec#lxd:} ;;
    ssh:*)    CHAOS_TT=ssh;     CHAOS_SSH_HOST=${spec#ssh:} ;;
    gcloud:*) CHAOS_TT=gcloud;  CHAOS_GCLOUD_INSTANCE=${spec#gcloud:} ;;
    *) chaos_fatal "unsupported --target: '$spec'" \
         "expected=local|lxd:<instance>|ssh:<[user@]host>|gcloud:<instance>" ;;
  esac
  local uid
  case $CHAOS_TT in
    lxd) CHAOS_ROOT_PREFIX="" ;;  # `lxc exec` is already root
    *)
      uid=$(chaos_t_exec 'id -u' 2>/dev/null || true)
      uid=${uid//[^0-9]/}
      if [ "${uid:-}" = "0" ]; then
        CHAOS_ROOT_PREFIX=""
      else
        CHAOS_ROOT_PREFIX="sudo -n "
      fi
      ;;
  esac
}

# chaos_t_exec CMD — run CMD as the login/exec user on the target.
chaos_t_exec() {
  local cmd=$1
  case $CHAOS_TT in
    local)  bash -c "$cmd" ;;
    lxd)    lxc exec "$CHAOS_LXD_INSTANCE" -- bash -c "$cmd" ;;
    ssh)
      # shellcheck disable=SC2086
      ssh ${CHAOS_SSH_OPTS:-} "$CHAOS_SSH_HOST" "$cmd" ;;
    gcloud)
      # shellcheck disable=SC2086
      gcloud compute ssh "$CHAOS_GCLOUD_INSTANCE" ${CHAOS_GCLOUD_OPTS:-} --command "$cmd" ;;
    *) return 127 ;;
  esac
}

# chaos_t_root CMD — run CMD with root on the target (no-op prefix when root).
chaos_t_root() {
  local cmd=$1
  if [ -n "${CHAOS_ROOT_PREFIX:-}" ]; then
    chaos_t_exec "${CHAOS_ROOT_PREFIX}bash -c $(printf '%q' "$cmd")"
  else
    chaos_t_exec "$cmd"
  fi
}

# chaos_t_has CMD_NAME — true if the command exists on the target.
chaos_t_has() {
  local name=$1 out
  out=$(chaos_t_exec "command -v '$name' >/dev/null 2>&1 && echo yes || echo no" 2>/dev/null || true)
  [ "$out" = "yes" ]
}

# chaos_t_klog_since ISO_TS — kernel log lines since ISO_TS (journald preferred).
chaos_t_klog_since() {
  local iso=$1
  chaos_t_root "journalctl -k --since '$iso' -o short-iso --no-pager 2>/dev/null || dmesg 2>/dev/null || true" 2>/dev/null || true
}

# chaos_parse_meminfo — read /proc/meminfo-style text on stdin, print:
#   used_pct avail_pct free_pct total_kb avail_kb free_kb swaptotal_kb avail_psi_some
# used_pct = 100*(MemTotal-MemAvailable)/MemTotal (the "committed" view; excludes
# reclaimable page cache) and matches what a node reports as ram_used_bytes when
# it derives usage from MemAvailable. avail_psi_some is /proc/pressure/memory
# "some avg10". Missing PSI yields 0.
chaos_parse_meminfo() {
  awk '
    /^MemTotal:/     { t=$2 }
    /^MemAvailable:/ { a=$2 }
    /^MemFree:/      { f=$2 }
    /^SwapTotal:/    { st=$2 }
    /^some/ && /avg10=/ { split($0, p, " "); psi=p[2]; sub(/avg10=/, "", psi) }
    END {
      if (t > 0) {
        printf "%.2f %.2f %.2f %d %d %d %d %.2f\n", \
          100*(t-a)/t, 100*a/t, 100*f/t, t, a, f, st, psi+0
      } else {
        printf "0 0 0 0 0 0 0 0\n"
      }
    }'
}

# =============================================================================
# end chaos_lib.sh
# =============================================================================
