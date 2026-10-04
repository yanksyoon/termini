#!/usr/bin/env bash
# termini-llama-serve.sh — first-boot CPU llama.cpp server for Node C.
#
# The generic Termini node image stays activation-only (D3). Node C is the one
# node that must actually serve a model for the demo, so this script is injected
# into Node C's cloud-init and builds a CPU-only llama.cpp plus a small quantized
# GGUF model, then runs llama-server on the frozen contract port.
#
# Frozen contract (docs/contracts/infra-decisions.md D2/D5):
#   * bind 0.0.0.0:8080 (the GCP firewall only allows 10.10.0.0/24 -> node-c:8080)
#   * CPU inference: small model (Qwen2.5-1.5B-Instruct Q4_K_M) sized for CPU
#
# The __PLACEHOLDER__ tokens are substituted by Terraform (main.tf) at apply time
# so this file stays a plain, lintable shell script.
set -euo pipefail

LLAMACPP_COMMIT="__LLAMACPP_COMMIT__"
MODEL_URL="__MODEL_URL__"
MODEL_FILE="__MODEL_FILE__"
PORT="__PORT__"

PREFIX="/opt/termini/llama.cpp"
MODEL_DIR="/var/lib/termini/models"
SRC="/usr/local/src/llama.cpp"
LOG="/var/log/termini-llama-serve.log"

mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
echo "[$(date -u +%FT%TZ)] termini-llama-serve: start (llama.cpp $LLAMACPP_COMMIT)"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends build-essential cmake git curl ca-certificates

if [ ! -d "$SRC/.git" ]; then
  git clone --quiet https://github.com/ggml-org/llama.cpp "$SRC"
fi
git -C "$SRC" fetch --quiet --depth 1 origin "$LLAMACPP_COMMIT"
git -C "$SRC" checkout --quiet "$LLAMACPP_COMMIT"
built="$(git -C "$SRC" rev-parse HEAD)"
if [ "$built" != "$LLAMACPP_COMMIT" ]; then
  echo "FATAL: llama.cpp checkout $built != pinned $LLAMACPP_COMMIT"
  exit 1
fi

# CPU-only, static, no libcurl: the resulting llama-server is self-contained.
cmake -S "$SRC" -B "$SRC/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_NATIVE=OFF \
  -DGGML_CUDA=OFF \
  -DBUILD_SHARED_LIBS=OFF \
  -DLLAMA_CURL=OFF
cmake --build "$SRC/build" --config Release -j "$(nproc)" --target llama-server

install -d -m 0755 "$PREFIX/bin"
install -m 0755 "$SRC/build/bin/llama-server" "$PREFIX/bin/llama-server"

# Model weights on local disk; no fetch at request time.
install -d -m 0755 "$MODEL_DIR"
if [ ! -s "$MODEL_DIR/$MODEL_FILE" ]; then
  echo "[$(date -u +%FT%TZ)] downloading $MODEL_FILE"
  curl -fL --retry 3 --retry-delay 2 -o "$MODEL_DIR/$MODEL_FILE.part" "$MODEL_URL"
  chmod 0644 "$MODEL_DIR/$MODEL_FILE.part"
  mv "$MODEL_DIR/$MODEL_FILE.part" "$MODEL_DIR/$MODEL_FILE"
fi
sha256sum "$MODEL_DIR/$MODEL_FILE" > "$MODEL_DIR/$MODEL_FILE.sha256"

id termini-infer >/dev/null 2>&1 || \
  useradd --system --no-create-home --shell /usr/sbin/nologin termini-infer

cat > /etc/systemd/system/termini-llama-server.service <<UNIT
[Unit]
Description=Termini CPU llama.cpp server (Node C, :$PORT)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=termini-infer
ExecStart=$PREFIX/bin/llama-server --model $MODEL_DIR/$MODEL_FILE --host 0.0.0.0 --port $PORT --ctx-size 4096 --parallel 2
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now termini-llama-server.service

echo "[$(date -u +%FT%TZ)] termini-llama-server enabled; health http://0.0.0.0:$PORT/health"
