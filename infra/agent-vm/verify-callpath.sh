#!/usr/bin/env bash
# verify-callpath.sh — cross-node agent->llama.cpp streaming acceptance (W4 / t_fd8eee6c).
#
# Proves the card's acceptance end to end, from inside a real Agent VM:
#
#   acceptance: "a multi-turn task from Node A completes with streaming tokens;
#                latency recorded"
#
# Lab topology: demo-node-a == Node A (compute), demo-node-c == Node C (inference,
# the frozen direct route http://<nodeC>:8080, agent-runtime.md D2/D5). The same
# script runs unchanged on real underlay nodes — only --node-a/--node-c change.
#
# Steps:
#   1. resolve the topology (Node C's underlay IP, the inference route);
#   2. ensure the baked agent image alias is present on Node A (placing it if not);
#   3. launch a fresh Agent VM on Node A from that local alias (offline, no pull);
#   4. install the current tree's termini-agent package into the VM;
#   5. run `termini-agent multiturn` — an ADK LlmAgent session whose every turn is
#      served by llama.cpp on Node C over the direct host route — and pull the JSON
#      report out;
#   6. assert (tools/check_callpath.py) every turn streamed >= 2 discrete deltas and
#      that first-token + total latency were recorded, then write the evidence;
#   7. tear the VM down.
#
# Exit 0 only when every assertion passes. Companion to verifier card t_9549b286.
#
# Usage:
#   infra/agent-vm/verify-callpath.sh [options]
#     --node-a NAME     compute node hosting the Agent VM (default demo-node-a)
#     --node-c NAME     inference node / Node C (default demo-node-c)
#     --port N          inference port (default 8080)
#     --instance NAME   agent VM instance name (default termini-callpath-$$)
#     --turns N         number of scripted turns (default 3)
#     --max-tokens N    per-turn generation cap (default 96)
#     --timeout S       wall-clock cap for the task (default 900)
#     --model ALIAS     model alias (default gemma — the alias Node C serves)
#     --evidence DIR    evidence dir (default <repo>/infra/agent-vm/evidence)
#     --keep            leave the agent VM running (post-mortem)
#     -h|--help
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SELF_DIR}/../.." && pwd)"
AGENT_SRC="${REPO_DIR}/image/agent-vm/agent"

NODE_A="${TERMINI_NODE_A:-demo-node-a}"
NODE_C="${TERMINI_NODE_C:-demo-node-c}"
PORT="${TERMINI_INFERENCE_PORT:-8080}"
MODEL="${TERMINI_MODEL_ALIAS:-gemma}"
INSTANCE="${TERMINI_CALLPATH_INSTANCE:-termini-callpath-$$}"
TURNS=3
MAX_TOKENS="${TERMINI_CALLPATH_MAX_TOKENS:-192}"
RUN_TIMEOUT="${TERMINI_CALLPATH_TIMEOUT:-900}"
EVIDENCE_DIR="${TERMINI_EVIDENCE_DIR:-${SELF_DIR}/evidence}"
NODE_ALIAS="${TERMINI_NODE_IMAGE_ALIAS:-agent-os/w5}"
PROFILE="${TERMINI_AGENT_PROFILE:-termini-agent}"
KEEP=0

log() { printf '[callpath] %s\n' "$*" >&2; }
die() { printf '[callpath] FATAL: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --node-a) NODE_A="$2"; shift 2 ;;
    --node-c) NODE_C="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --instance) INSTANCE="$2"; shift 2 ;;
    --turns) TURNS="$2"; shift 2 ;;
    --max-tokens) MAX_TOKENS="$2"; shift 2 ;;
    --timeout) RUN_TIMEOUT="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --evidence) EVIDENCE_DIR="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

node_exec() { local n="$1"; shift; lxc exec "$n" -- "$@"; }
inst_exec() { local n="$1" i="$2"; shift 2; lxc exec "$n" -- lxc exec "$i" -- "$@"; }

node_ipv4() {
  lxc list "$1" --format csv -c 4 2>/dev/null | tr ',' '\n' | awk '/^[0-9]+\./{print $1; exit}'
}
inst_ipv4() {
  node_exec "$1" lxc list "$2" --format csv -c 4 2>/dev/null | tr ',' '\n' \
    | awk '/^[0-9]+\./{print $1; exit}'
}

# Copy a local file into the instance via the node (nested LXD). A nested file
# push can report a spurious non-zero after a successful transfer, so verify by size.
push_into_instance() { # NODE INSTANCE LOCAL_FILE PATH_IN_INSTANCE
  local n="$1" i="$2" local_file="$3" remote_path="$4" tmp_name
  tmp_name="$(basename "$remote_path").$$.$RANDOM"
  lxc file push "$local_file" "${n}/root/${tmp_name}" >/dev/null
  node_exec "$n" lxc file push "/root/${tmp_name}" "${i}/${remote_path}" >/dev/null 2>&1 || true
  inst_exec "$n" "$i" test -s "/${remote_path}" || die "push of ${remote_path} failed"
  node_exec "$n" rm -f "/root/${tmp_name}" >/dev/null 2>&1 || true
}

PKG_TMP=""
cleanup() {
  [ -n "$PKG_TMP" ] && rm -rf "$PKG_TMP" || true
  if [ "$KEEP" = 0 ]; then
    node_exec "$NODE_A" lxc delete "$INSTANCE" --force >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# --- 1. topology -------------------------------------------------------------
lxc info "$NODE_A" >/dev/null 2>&1 || die "Node A (${NODE_A}) not found in LXD"
lxc info "$NODE_C" >/dev/null 2>&1 || die "Node C (${NODE_C}) not found in LXD"
NODE_A_IP="$(node_ipv4 "$NODE_A")"
NODE_C_IP="$(node_ipv4 "$NODE_C")"
[ -n "$NODE_C_IP" ] || die "could not resolve Node C (${NODE_C}) IPv4"
NODE_C_NET="${NODE_C_IP%.*}.0/24"
INFERENCE_URL="http://${NODE_C_IP}:${PORT}"
log "Node A=${NODE_A} (${NODE_A_IP:-?})  Node C=${NODE_C} (${NODE_C_IP})  inference=${INFERENCE_URL}"

# --- 2. agent image present locally on Node A --------------------------------
if ! node_exec "$NODE_A" lxc image info "$NODE_ALIAS" >/dev/null 2>&1; then
  log "alias ${NODE_ALIAS} absent on ${NODE_A}; placing it"
  "${SELF_DIR}/place-agent-image.sh" --nodes "$NODE_A" >/dev/null
fi
IMG_FP="$(node_exec "$NODE_A" lxc image info "$NODE_ALIAS" 2>/dev/null \
  | awk '/^Fingerprint:/{print $2}')"
log "agent image ${NODE_ALIAS} fingerprint=${IMG_FP:-?} on ${NODE_A}"

# --- 3. launch a fresh Agent VM on Node A ------------------------------------
node_exec "$NODE_A" lxc delete "$INSTANCE" --force >/dev/null 2>&1 || true
log "launching ${INSTANCE} on ${NODE_A} from the local image (no pull)"
node_exec "$NODE_A" lxc launch "$NODE_ALIAS" "$INSTANCE" -p "$PROFILE" >/dev/null

log "waiting for the VM to boot and acquire an address"
INST_IP=""
for _ in $(seq 1 120); do
  INST_IP="$(inst_ipv4 "$NODE_A" "$INSTANCE")"
  [ -n "$INST_IP" ] && break
  sleep 1
done
[ -n "$INST_IP" ] || die "agent VM ${INSTANCE} never got an IPv4"
log "agent VM ${INSTANCE} up at ${INST_IP}"

for _ in $(seq 1 90); do
  inst_exec "$NODE_A" "$INSTANCE" test -x /opt/termini/venv/bin/python && break
  sleep 1
done
inst_exec "$NODE_A" "$INSTANCE" test -x /opt/termini/venv/bin/python \
  || die "runtime venv missing inside ${INSTANCE}"

# --- 4. install the current tree's termini-agent into the VM -----------------
PKG_TMP="$(mktemp -d)"
tar -C "$(dirname "$AGENT_SRC")" -czf "${PKG_TMP}/agent-src.tgz" "$(basename "$AGENT_SRC")"
push_into_instance "$NODE_A" "$INSTANCE" "${PKG_TMP}/agent-src.tgz" "tmp/agent-src.tgz"
inst_exec "$NODE_A" "$INSTANCE" bash -lc '
  rm -rf /opt/termini/ws-src && mkdir -p /opt/termini/ws-src
  tar -C /opt/termini/ws-src --strip-components=1 -xzf /tmp/agent-src.tgz
  test -f /opt/termini/ws-src/src/termini_agent/cli.py'

# --- 5. build inputs + run the multi-turn ADK task ---------------------------
INST_CFG="${PKG_TMP}/callpath-agent.yaml"
cat > "$INST_CFG" <<YAML
project_id: 01J00000000000000000000000
vm_id: 01JCALLPATH0000000000000V1
node_id: 01JCALLPATH0000000000000N1
agent_image_version: agent-vm-2026.10.0
inference:
  base_url: ${INFERENCE_URL}
  default_model: ${MODEL}
  allowed_models: [${MODEL}]
  allowed_networks: [10.10.0.0/24, ${NODE_C_NET}]
  token_file: /etc/termini/inference.token
  stream: true
  connect_timeout_s: 5
  request_timeout_s: 600
broker:
  endpoint: http://10.20.1.1:7071
  pull_interval_s: 1
  max_concurrent_tasks: 2
logging:
  sink: /tmp/agent.jsonl
  local_only: true
YAML

TURNS_JSON="${PKG_TMP}/callpath-turns.json"
python3 - "$TURNS" > "$TURNS_JSON" <<'PY'
import json, sys
n = int(sys.argv[1])
scripted = [
    "Name the first letter of the Greek alphabet in one short sentence.",
    "Now name the second letter of the Greek alphabet.",
    "In one sentence, summarise the letters you have named so far.",
    "Name the fourth letter of the Greek alphabet.",
    "In one sentence, confirm every Greek letter you have named.",
]
turns = scripted[:n] if n <= len(scripted) else scripted + [f"Continue the session: turn {i}." for i in range(len(scripted) + 1, n + 1)]
print(json.dumps(turns))
PY

push_into_instance "$NODE_A" "$INSTANCE" "$INST_CFG" "tmp/callpath-agent.yaml"
push_into_instance "$NODE_A" "$INSTANCE" "$TURNS_JSON" "tmp/callpath-turns.json"
push_into_instance "$NODE_A" "$INSTANCE" "${SELF_DIR}/tools/callpath-runner.sh" "tmp/callpath-runner.sh"

install -d -m 0755 "$EVIDENCE_DIR"
RUN_LOG="${EVIDENCE_DIR}/callpath-run.txt"
log "running the multi-turn ADK task inside ${INSTANCE} (timeout ${RUN_TIMEOUT}s)"
set +e
timeout "$RUN_TIMEOUT" lxc exec "$NODE_A" -- lxc exec "$INSTANCE" -- bash /tmp/callpath-runner.sh \
  "${NODE_C_IP}" "${PORT}" "${MAX_TOKENS}" > "$RUN_LOG" 2>&1
RUN_RC=$?
set -e
cat "$RUN_LOG" >&2 || true

# --- pull the artifacts out ---------------------------------------------------
pull_inst_file() { # SRC_IN_INST DEST
  local tmp; tmp="$(basename "$2").$$"
  node_exec "$NODE_A" lxc file pull "${INSTANCE}/$1" "/root/${tmp}" >/dev/null 2>&1 || return 1
  lxc file pull "${NODE_A}/root/${tmp}" "$2" >/dev/null 2>&1 || true
  node_exec "$NODE_A" rm -f "/root/${tmp}" >/dev/null 2>&1 || true
}
pull_inst_file tmp/callpath-report.json "${EVIDENCE_DIR}/callpath-report.json"
pull_inst_file tmp/env.json "${EVIDENCE_DIR}/callpath-env.json"
pull_inst_file tmp/probe.json "${EVIDENCE_DIR}/callpath-probe.json"

# --- 6. assert the acceptance -------------------------------------------------
set +e
python3 "${SELF_DIR}/tools/check_callpath.py" \
  --report "${EVIDENCE_DIR}/callpath-report.json" \
  --env "${EVIDENCE_DIR}/callpath-env.json" \
  --probe "${EVIDENCE_DIR}/callpath-probe.json" \
  --expect-turns "$TURNS" \
  --min-chunks-per-turn 2 \
  --node-a "$NODE_A" --node-c "$NODE_C" --node-c-ip "$NODE_C_IP" \
  --inference-url "$INFERENCE_URL" --model "$MODEL" \
  --image-alias "$NODE_ALIAS" --image-fingerprint "$IMG_FP" \
  --run-log "$RUN_LOG" \
  --out "${EVIDENCE_DIR}/callpath-evidence.json"
ASSERT_RC=$?
set -e

if [ "$ASSERT_RC" -eq 0 ] && [ "$RUN_RC" -eq 0 ]; then
  echo "CALLPATH: PASS (evidence: ${EVIDENCE_DIR}/callpath-evidence.json)"
  exit 0
fi
echo "CALLPATH: FAIL (run_rc=${RUN_RC}, assert_rc=${ASSERT_RC})" >&2
exit 1
