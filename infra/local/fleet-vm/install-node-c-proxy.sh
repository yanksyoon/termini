#!/bin/bash
# install-node-c-proxy.sh — the inference FRONT DOOR on a node that is not a GCE
# instance (a local LXD VM), with the forwarded (`gemini-proxy`) backend.
#
# The shipped installer (infra/inference/install-inference-proxy.sh) assumes the
# node went through first boot and therefore has the CPU llama.cpp worker and the
# pinned GGUF on disk; it refuses to run otherwise. A local VM is built from a
# stock Ubuntu cloud image instead, so there is no worker and no weights. What the
# SHIPPED configuration actually needs is the front door only: `gemini-proxy`
# forwards every completion to Vertex AI and never reads the worker.
#
# So this installs exactly the front door — the same unit, the same env keys, the
# same paths — and says plainly what it did NOT install:
#   * no termini-llama-server.service (no weights on this node);
#   * therefore `termini-inference-switch local` / `legacy` are NOT available
#     here until the CPU path is built (infra/gcp/cloud-init/llama-serve.sh).
#
# Identity: the proxy mints its OAuth token from the GCE metadata server, which a
# local VM does not have. This installer therefore also installs
# termini-vertex-token-refresh.{service,timer}, which mints the same token from
# /etc/termini/vertex-sa.json and rotates it every 40 minutes.
#
# Run ON the node-c VM as root:
#   ./install-node-c-proxy.sh [--project PROJECT] [--port 8080]
set -euo pipefail

PROJECT_ID="${TERMINI_GEMINI_PROJECT:-termini-510609}"
PROXY_PORT=8080
MODEL_ALIAS=gemma
GEMINI_MODEL=gemini-2.5-flash
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --project) PROJECT_ID="$2"; shift ;;
    --port) PROXY_PORT="$2"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

PROXY_SRC="$REPO_ROOT/infra/inference/proxy.py"
UNIT_SRC="$REPO_ROOT/infra/inference/termini-inference-proxy.service"
BIN_DIR=/opt/termini/inference
ENV_DIR=/etc/termini

log() { printf '[node-c-proxy] %s\n' "$*"; }
[ -f "$PROXY_SRC" ] || { echo "missing $PROXY_SRC" >&2; exit 1; }
[ -f "$UNIT_SRC" ] || { echo "missing $UNIT_SRC" >&2; exit 1; }

id termini-infer >/dev/null 2>&1 || \
  useradd --system --no-create-home --shell /usr/sbin/nologin termini-infer

install -d -m 0755 "$BIN_DIR" "$ENV_DIR"
install -m 0755 "$PROXY_SRC" "$BIN_DIR/proxy.py"
/usr/bin/python3 "$BIN_DIR/proxy.py" --check >/dev/null
log "installed $BIN_DIR/proxy.py"

cat > "$ENV_DIR/inference-proxy.env" <<EOF
# Which backend answers on :$PROXY_PORT.
# gemini-proxy : translate to Vertex AI $GEMINI_MODEL and label the substitution
# local        : passthrough to the llama.cpp worker (NOT installed on this node)
TERMINI_INFERENCE_BACKEND=gemini-proxy
TERMINI_INFERENCE_HOST=0.0.0.0
TERMINI_INFERENCE_PORT=$PROXY_PORT
TERMINI_INFERENCE_ALIAS=$MODEL_ALIAS
TERMINI_INFERENCE_UPSTREAM=http://127.0.0.1:8081
TERMINI_INFERENCE_UPSTREAM_TIMEOUT_S=900
TERMINI_GEMINI_HOST=aiplatform.googleapis.com
TERMINI_GEMINI_LOCATION=global
TERMINI_GEMINI_MODEL=$GEMINI_MODEL
TERMINI_GEMINI_PROJECT=$PROJECT_ID
TERMINI_GEMINI_TIMEOUT_S=60
TERMINI_GEMINI_THINKING_BUDGET=0
TERMINI_INFERENCE_STATE_FILE=/run/termini/inference-backend.json
TERMINI_INFERENCE_LOG_FILE=/var/log/termini-inference-proxy.jsonl
EOF

install -d -m 0755 /run/termini /var/log
chown termini-infer:termini-infer /run/termini
install -m 0644 /dev/null /var/log/termini-inference-proxy.jsonl
chown termini-infer:termini-infer /var/log/termini-inference-proxy.jsonl

install -m 0644 "$UNIT_SRC" /etc/systemd/system/termini-inference-proxy.service

cat > /etc/systemd/system/termini-inference.conf.tmp <<EOF
d /run/termini 0755 termini-infer termini-infer -
EOF
install -m 0644 /etc/systemd/system/termini-inference.conf.tmp /usr/lib/tmpfiles.d/termini-inference.conf
rm -f /etc/systemd/system/termini-inference.conf.tmp

# --- token rotation (the metadata-identity replacement) --------------------- #
install -m 0755 "$HERE/termini-vertex-token-refresh.sh" /usr/local/sbin/termini-vertex-token-refresh

cat > /etc/systemd/system/termini-vertex-token-refresh.service <<'EOF'
[Unit]
Description=Mint/rotate the Vertex AI access token for the interference front door
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=GEMINI_PROJECT=__PROJECT__
Environment=SA_FILE=/etc/termini/vertex-sa.json
Environment=ENV_FILE=/etc/termini/inference-proxy.env
ExecStart=/usr/local/sbin/termini-vertex-token-refresh
EOF
sed -i "s|__PROJECT__|$PROJECT_ID|" /etc/systemd/system/termini-vertex-token-refresh.service

cat > /etc/systemd/system/termini-vertex-token-refresh.timer <<'EOF'
[Unit]
Description=Rotate the Vertex AI access token every 40 minutes

[Timer]
OnBootSec=30s
OnUnitActiveSec=40min
Unit=termini-vertex-token-refresh.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now termini-vertex-token-refresh.timer
systemctl start termini-vertex-token-refresh.service || true
systemctl enable termini-inference-proxy.service
systemctl restart termini-inference-proxy.service

log "waiting for the front door on :$PROXY_PORT"
for i in $(seq 1 30); do
  if curl -fsS -m 2 "http://127.0.0.1:$PROXY_PORT/health" >/dev/null 2>&1; then
    log "front door answered after ${i}s"
    break
  fi
  sleep 1
done
curl -sS -m 5 "http://127.0.0.1:$PROXY_PORT/health" || true
echo
log "NOT installed: llama.cpp worker + weights (gemini-proxy does not use them)"
