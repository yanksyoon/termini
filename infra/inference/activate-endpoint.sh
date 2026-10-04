#!/usr/bin/env bash
# activate-endpoint.sh — (re)activate the host-exposed llama.cpp inference
# endpoint on Node C. Idempotent; safe to re-run. Run ON Node C (after first
# boot installed the pinned binary + weights).
#
#   sudo infra/inference/activate-endpoint.sh
#
# Env overrides:
#   BIN         llama-server path        (default /opt/termini/llama.cpp/bin/llama-server)
#   MODEL_DIR   weights dir              (default /var/lib/termini/models)
#   MODEL_FILE  GGUF filename            (default DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf)
#   MODEL_ALIAS advertised alias         (default deepseek-flash)
#   PORT        listen port              (default 8080)
#   CTX_SIZE    context size             (default 8192)
#   PARALLEL    server slots             (default 2)
#   UNIT        systemd unit path        (default /etc/systemd/system/termini-llama-server.service)
set -euo pipefail

BIN="${BIN:-/opt/termini/llama.cpp/bin/llama-server}"
MODEL_DIR="${MODEL_DIR:-/var/lib/termini/models}"
MODEL_FILE="${MODEL_FILE:-DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf}"
MODEL_ALIAS="${MODEL_ALIAS:-deepseek-flash}"
PORT="${PORT:-8080}"
CTX_SIZE="${CTX_SIZE:-8192}"
PARALLEL="${PARALLEL:-2}"
UNIT="${UNIT:-/etc/systemd/system/termini-llama-server.service}"

[ -x "$BIN" ] || { echo "FATAL: $BIN missing (run the cloud-init first-boot script)" >&2; exit 1; }
[ -s "$MODEL_DIR/$MODEL_FILE" ] || { echo "FATAL: model $MODEL_DIR/$MODEL_FILE missing" >&2; exit 1; }

echo "== sha256 of $MODEL_FILE =="
sha256sum "$MODEL_DIR/$MODEL_FILE"

id termini-infer >/dev/null 2>&1 || \
  useradd --system --no-create-home --shell /usr/sbin/nologin termini-infer

echo "== writing $UNIT =="
tee "$UNIT" >/dev/null <<UNITEOF
[Unit]
Description=Termini host-exposed llama.cpp inference server (Node C, :$PORT)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=termini-infer
ExecStart=$BIN --model $MODEL_DIR/$MODEL_FILE --alias $MODEL_ALIAS --host 0.0.0.0 --port $PORT --ctx-size $CTX_SIZE --parallel $PARALLEL --reasoning-format none
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNITEOF

systemctl daemon-reload
systemctl enable termini-llama-server.service
systemctl restart termini-llama-server.service

echo "== waiting for /health =="
for i in $(seq 1 90); do
  if curl -s -m 2 "http://127.0.0.1:${PORT}/health" 2>/dev/null | grep -q '"ok"'; then
    echo "ready after ${i}s"; break
  fi
  sleep 1
done

echo "== status =="
systemctl is-enabled termini-llama-server.service
systemctl is-active termini-llama-server.service
echo "== listener =="
ss -lntp 2>/dev/null | grep ":${PORT}" || true
echo "== /v1/models =="
curl -s -m 5 "http://127.0.0.1:${PORT}/v1/models" | head -c 600; echo
echo "ACTIVATE_DONE"
