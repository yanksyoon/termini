#!/usr/bin/env bash
# install-inference-proxy.sh — put the simulated-GPU front door on Node C.
#
# Run ON Node C as root, on a node whose first boot already built llama.cpp and
# fetched the pinned weights (infra/gcp/cloud-init/llama-serve.sh):
#
#   sudo ./install-inference-proxy.sh [--backend local|gemini-proxy]
#
# What it changes (and why each change is needed):
#
#   1. `/opt/termini/inference/proxy.py` — the proxy itself.
#   2. `termini-llama-server.service` becomes a LOOPBACK WORKER
#      (`127.0.0.1:8081`) instead of the host-exposed server on `0.0.0.0:8080`.
#      This is the "disable the stale host-exposed serve path" step, and it also
#      realises the frozen AR-4 shape (front door on :8080, workers on loopback).
#      The original unit is backed up next to it and `termini-inference-switch
#      legacy` puts the node back exactly as it was — one command, no reinstall.
#   3. `termini-inference-proxy.service` — the front door on `0.0.0.0:8080`,
#      i.e. the address nodes A/B already call. They cannot tell the difference.
#   4. `/etc/termini/inference-proxy.env` — the backend choice. Configuration, not
#      code: `termini-inference-switch local` restores the CPU path, no rebuild.
#
# Idempotent: re-running repairs the units and re-asserts the chosen backend.
set -euo pipefail

BACKEND="${BACKEND:-gemini-proxy}"
ALIAS="${ALIAS:-gemma}"
PROXY_PORT="${PROXY_PORT:-8080}"
WORKER_PORT="${WORKER_PORT:-8081}"
PREFIX="${PREFIX:-/opt/termini/llama.cpp}"
BIN="${BIN:-$PREFIX/bin/llama-server}"
MODEL_DIR="${MODEL_DIR:-/var/lib/termini/models}"
MODEL_FILE="${MODEL_FILE:-gemma-3-4b-it-Q4_K_M.gguf}"
BIN_DIR="${BIN_DIR:-/opt/termini/inference}"
ENV_DIR="${ENV_DIR:-/etc/termini}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"
GEMINI_LOCATION="${GEMINI_LOCATION:-global}"
GEMINI_HOST="${GEMINI_HOST:-aiplatform.googleapis.com}"
GEMINI_TIMEOUT_S="${GEMINI_TIMEOUT_S:-30}"
GEMINI_THINKING_BUDGET="${GEMINI_THINKING_BUDGET:-0}"
UPSTREAM_TIMEOUT_S="${UPSTREAM_TIMEOUT_S:-900}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --backend) BACKEND="$2"; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

case "$BACKEND" in
  local|gemini-proxy) : ;;
  *) echo "FATAL: --backend must be local or gemini-proxy (got '$BACKEND')" >&2; exit 2 ;;
esac

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROXY_SRC="${PROXY_SRC:-$HERE/proxy.py}"
UNIT_SRC="${UNIT_SRC:-$HERE/termini-inference-proxy.service}"

log() { printf '[install-inference-proxy] %s\n' "$*" >&2; }
die() { printf '[install-inference-proxy] FATAL: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "run as root (it writes systemd units)"
[ -f "$PROXY_SRC" ] || die "proxy source not found: $PROXY_SRC"
[ -f "$UNIT_SRC" ] || die "unit source not found: $UNIT_SRC"
[ -x "$BIN" ] || die "$BIN missing — run the node's first-boot llama.cpp build first"
[ -s "$MODEL_DIR/$MODEL_FILE" ] || die "model $MODEL_DIR/$MODEL_FILE missing on this node"

id termini-infer >/dev/null 2>&1 || \
  useradd --system --no-create-home --shell /usr/sbin/nologin termini-infer

# --- 1. the proxy ------------------------------------------------------------ #
install -d -m 0755 "$BIN_DIR"
install -m 0755 "$PROXY_SRC" "$BIN_DIR/proxy.py"
/usr/bin/python3 "$BIN_DIR/proxy.py" --check >/dev/null \
  || die "the proxy does not start cleanly (--check failed)"
log "installed $BIN_DIR/proxy.py"

# --- 2. the llama.cpp worker moves to loopback ------------------------------- #
install -d -m 0755 "$ENV_DIR"
cat > "$ENV_DIR/llama-worker.env" <<EOF
# Termini Node C llama.cpp WORKER (the "local" backend of the inference proxy).
# Loopback by design: the front door on :$PROXY_PORT is the only listener that is
# reachable from the underlay, exactly as AR-4 describes. Changing LLAMA_HOST/
# LLAMA_PORT back to 0.0.0.0/$PROXY_PORT is what 'termini-inference-switch legacy'
# does — that is the one-step restore of the pre-proxy serve path.
LLAMA_HOST=127.0.0.1
LLAMA_PORT=$WORKER_PORT
EOF

# Keep the shipped weights/flags; only the bind address becomes configurable.
cat > "$UNIT_DIR/termini-llama-server.service" <<EOF
[Unit]
Description=Termini llama.cpp worker (Node C, loopback :\$LLAMA_PORT) — the proxy's local backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=termini-infer
EnvironmentFile=$ENV_DIR/llama-worker.env
ExecStart=$BIN --model $MODEL_DIR/$MODEL_FILE --alias $ALIAS --host \$LLAMA_HOST --port \$LLAMA_PORT --ctx-size 32768 --parallel 2 --threads 6 --reasoning-format none
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
log "termini-llama-server.service -> worker on 127.0.0.1:$WORKER_PORT (host-exposed :$PROXY_PORT released)"

# --- 3. the front door ------------------------------------------------------- #
install -m 0644 "$UNIT_SRC" "$UNIT_DIR/termini-inference-proxy.service"

# --- 4. the backend choice (configuration, not code) ------------------------- #
cat > "$ENV_DIR/inference-proxy.env" <<EOF
# Which backend answers on :$PROXY_PORT. THIS is the switch — no rebuild.
#   gemini-proxy : translate to Vertex AI $GEMINI_MODEL and label the substitution
#   local        : transparent passthrough to the llama.cpp worker on 127.0.0.1:$WORKER_PORT
TERMINI_INFERENCE_BACKEND=$BACKEND
TERMINI_INFERENCE_HOST=0.0.0.0
TERMINI_INFERENCE_PORT=$PROXY_PORT
TERMINI_INFERENCE_ALIAS=$ALIAS
TERMINI_INFERENCE_UPSTREAM=http://127.0.0.1:$WORKER_PORT
TERMINI_INFERENCE_UPSTREAM_TIMEOUT_S=$UPSTREAM_TIMEOUT_S
# Vertex AI. The project id is read from the instance metadata server when empty
# (no project constant baked into the node), and the access token is minted per
# request from the same metadata server — there is no key file on this node.
TERMINI_GEMINI_HOST=$GEMINI_HOST
TERMINI_GEMINI_LOCATION=$GEMINI_LOCATION
TERMINI_GEMINI_MODEL=$GEMINI_MODEL
TERMINI_GEMINI_PROJECT=
TERMINI_GEMINI_TIMEOUT_S=$GEMINI_TIMEOUT_S
TERMINI_GEMINI_THINKING_BUDGET=$GEMINI_THINKING_BUDGET
TERMINI_INFERENCE_STATE_FILE=/run/termini/inference-backend.json
TERMINI_INFERENCE_LOG_FILE=/var/log/termini-inference-proxy.jsonl
EOF
install -d -m 0755 /run/termini /var/log
# The proxy runs as termini-infer and records the ACTIVE backend in
# /run/termini/inference-backend.json — the node's status surface for "which one
# is serving". It needs write access to that directory.
chown termini-infer:termini-infer /run/termini
chmod 0755 /run/termini
install -m 0644 /dev/null /var/log/termini-inference-proxy.jsonl
chown termini-infer:termini-infer /var/log/termini-inference-proxy.jsonl

systemctl daemon-reload
systemctl enable termini-llama-server.service >/dev/null 2>&1 || true
systemctl restart termini-llama-server.service
systemctl enable termini-inference-proxy.service >/dev/null 2>&1
systemctl restart termini-inference-proxy.service

log "waiting for the front door on :$PROXY_PORT"
for i in $(seq 1 60); do
  if curl -fsS -m 2 "http://127.0.0.1:$PROXY_PORT/health" >/dev/null 2>&1; then
    log "front door answered after ${i}s"
    break
  fi
  sleep 1
done

log "active backend:"
curl -fsS -m 5 "http://127.0.0.1:$PROXY_PORT/health" || true
echo
log "install done — 'termini-inference-switch status' reports the active backend"
