#!/usr/bin/env bash
# lib.sh — shared configuration + helpers for the Termini demo warm layer.
# Sourced by the other scripts (never executed directly).
#
# Everything the demo touches is pinned here: the image alias, the node set,
# the controller endpoint, the resident model, and the snapshot names. If a
# value must change, change it here and re-run `demo-env.sh up`.

# shellcheck shell=bash
# shellcheck disable=SC2034  # many config vars are consumed by the sourcing scripts
set -euo pipefail

WARM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$WARM_DIR/../.." && pwd)"

# --- pinned image identity (must match image/models.lock.json + README) ------
IMAGE_ALIAS="${TERMINI_IMAGE_ALIAS:-agentic-os/w3}"
IMAGE_VERSION="${TERMINI_IMAGE_VERSION:-2026.10.0}"

# --- local state / build paths ----------------------------------------------
TERMINI_STATE="${TERMINI_STATE:-/var/lib/termini}"
STATE_DIR="$TERMINI_STATE/demo"
BUILD_DIR="$TERMINI_STATE/build"
MODELS_DIR="$TERMINI_STATE/models"
LLAMA_BIN_DIR="$BUILD_DIR/llamacpp/bin"
PUBLISH_DIR="$BUILD_DIR/published"

# --- controller --------------------------------------------------------------
CONTROLLER_PORT="${TERMINI_CONTROLLER_PORT:-8085}"
CONTROLLER_BIND="${TERMINI_CONTROLLER_BIND:-0.0.0.0}"
# Nodes reach the host through the LXD bridge gateway.
BRIDGE="${TERMINI_BRIDGE:-lxdbr0}"
BRIDGE_GW="${TERMINI_BRIDGE_GW:-10.14.100.1}"
CONTROLLER_URL_NODE="${TERMINI_CONTROLLER_URL_NODE:-http://${BRIDGE_GW}:${CONTROLLER_PORT}}"
CONTROLLER_URL_HOST="http://127.0.0.1:${CONTROLLER_PORT}"
PROJECT_TOKEN="${TERMINI_PROJECT_TOKEN:-term-demo-project-token-000000000001}"
PROJECT_ID="${TERMINI_PROJECT_ID:-01J0DEM0WARM000000000000AB}"
BOOTSTRAP_TOKENS="${TERMINI_BOOTSTRAP_TOKENS:-term-demo-boot-a,term-demo-boot-b,term-demo-boot-c,term-demo-boot-fresh}"
CONTROLLER_DB="${TERMINI_DB_PATH:-$STATE_DIR/controller.db}"
CONTROLLER_LOG="$STATE_DIR/controller.log"
CONTROLLER_PIDFILE="$STATE_DIR/controller.pid"

# --- demo nodes: "name:role:token" ------------------------------------------
DEMO_NODES=(
  "demo-node-a:compute:term-demo-boot-a"
  "demo-node-b:compute:term-demo-boot-b"
  "demo-node-c:inference:term-demo-boot-c"
)
FRESH_NAME="${TERMINI_FRESH_NAME:-demo-node-fresh}"
FRESH_TOKEN="term-demo-boot-fresh"

SNAPSHOT_WARM="${TERMINI_SNAPSHOT_WARM:-warm}"       # registered + inference resident
SNAPSHOT_UNREG="${TERMINI_SNAPSHOT_UNREG:-unregistered}"  # baked, not yet enrolled

# --- resident model (pinned by image/models.lock.json) -----------------------
MODEL_ALIAS="${TERMINI_MODEL_ALIAS:-deepseek-flash}"
MODEL_FILE="${TERMINI_MODEL_FILE:-DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf}"
MODEL_SIZE_BYTES="${TERMINI_MODEL_SIZE_BYTES:-4920736608}"
MODEL_SHA256="${TERMINI_MODEL_SHA256:-87bcba20b4846d8dadf753d3ff48f9285d131fc95e3e0e7e934d4f20bc896f5d}"
INFERENCE_PORT="${TERMINI_INFERENCE_PORT:-8080}"
# llama.cpp loads the model with --no-mmap, so VmRSS tracks residency. Require
# at least ~60% of the on-disk model to be resident before we call it warm.
RESIDENT_RSS_MIN_BYTES="${TERMINI_RESIDENT_RSS_MIN_BYTES:-3000000000}"

# --- Scenario-B scratch volumes ---------------------------------------------
SCRATCH_VOLUME="${TERMINI_SCRATCH_VOLUME:-demo-scratch-a}"
SCRATCH_VOLUME_B="${TERMINI_SCRATCH_VOLUME_B:-demo-scratch-b}"
SCRATCH_VOLUME_C="${TERMINI_SCRATCH_VOLUME_C:-demo-scratch-c}"
SCRATCH_MOUNT="/mnt/termini-scratch"
SCRATCH_SIZE="${TERMINI_SCRATCH_SIZE:-2GiB}"

# --- registration timing budget ---------------------------------------------
REGISTER_BUDGET_S="${TERMINI_REGISTER_BUDGET_S:-30}"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
# Human-facing progress goes to stderr; stdout carries only data (e.g. the
# token/seconds a caller captures with $(...)).
log()  { printf '[warm] %s\n' "$*" >&2; }
warn() { printf '[warm][warn] %s\n' "$*" >&2; }
die()  { printf '[warm][FATAL] %s\n' "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "required tool not found: $1"; }

# node_role NAME -> prints role
node_role() {
  local n="$1" entry
  for entry in "${DEMO_NODES[@]}"; do
    if [ "${entry%%:*}" = "$n" ]; then printf '%s\n' "$(echo "$entry" | cut -d: -f2)"; return 0; fi
  done
  return 1
}

# node_token NAME -> prints bootstrap token
node_token() {
  local n="$1" entry
  for entry in "${DEMO_NODES[@]}"; do
    if [ "${entry%%:*}" = "$n" ]; then printf '%s\n' "$(echo "$entry" | cut -d: -f3)"; return 0; fi
  done
  return 1
}

node_names() {
  local entry
  for entry in "${DEMO_NODES[@]}"; do printf '%s\n' "${entry%%:*}"; done
}

# node_node_id NAME -> the controller node_id persisted by the node agent
node_node_id() {
  lxc exec "$1" -- /usr/local/bin/termini-bootstrap status </dev/null 2>/dev/null \
  | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("node_id") or "")
except Exception:
    print("")'
}

lxc_running() { # NAME
  lxc list "$1" --format csv -c s 2>/dev/null | grep -q '^RUNNING$'
}

node_stopped() { # NAME — 0 when the instance is not RUNNING
  ! lxc_running "$1"
}

snapshot_exists() { # NAME SNAP — 0 when the snapshot is present
  lxc query "/1.0/instances/$1/snapshots" 2>/dev/null \
    | jq -e --arg s "$2" 'any(.[]; endswith("/" + $s))' >/dev/null 2>&1
}

lxc_exists() { # NAME
  lxc info "$1" >/dev/null 2>&1
}

# wait_until SECONDS 'description' -- command...  (command must succeed)
wait_until() {
  local budget="$1" desc="$2"; shift 2
  local start now
  start="$(date +%s)"
  while :; do
    if "$@"; then return 0; fi
    now="$(date +%s)"
    if [ $((now - start)) -ge "$budget" ]; then
      warn "timeout after ${budget}s waiting for: $desc"
      return 1
    fi
    sleep 0.5
  done
}

# controller_up -> 0 if the controller answers
controller_up() {
  curl -fsS -m 2 "$CONTROLLER_URL_HOST/v1/telemetry/recent" \
    -H "Authorization: Bearer $PROJECT_TOKEN" >/dev/null 2>&1
}

# project_id_from_db -> the project id the controller persisted
project_id_from_db() {
  python3 - "$CONTROLLER_DB" <<'PY'
import sqlite3, sys
try:
    con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    row = con.execute("SELECT id FROM projects ORDER BY created_at LIMIT 1").fetchone()
    print(row[0] if row else "")
except Exception:
    print("")
PY
}

# image_pinned -> 0 if the alias resolves to an image
image_pinned() {
  lxc image list "$IMAGE_ALIAS" --format csv -c f 2>/dev/null | grep -q .
}

require_pinned_image() {
  image_pinned || die "image alias '$IMAGE_ALIAS' is not pinned in the local store; run: $WARM_DIR/bake-image.sh"
}

# warm_model_cache — read the pinned GGUF file into the host page cache.
# The inference node bind-mounts the model from the host, so a node that
# (re)starts its llama-server then loads the weights from RAM instead of a cold
# disk read (14 s -> sub-second on this host). This is the "preload the GGUF
# into page cache" warm path and it performs no network fetch.
warm_model_cache() {
  local f="$MODELS_DIR/$MODEL_FILE"
  [ -f "$f" ] || { warn "model cache warm skipped: $f absent"; return 0; }
  if command -v vmtouch >/dev/null 2>&1; then
    vmtouch -tq "$f" >/dev/null 2>&1 || true
  else
    # touch every page of the file; discard the bytes
    cat "$f" >/dev/null 2>&1 || true
  fi
}
