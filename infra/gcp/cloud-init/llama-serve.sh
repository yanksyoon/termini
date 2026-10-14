#!/usr/bin/env bash
# termini-llama-serve.sh — first-boot host-exposed llama.cpp server for Node C.
#
# The generic Termini node image stays activation-only (D3). Node C is the one
# node that must actually serve a model for the demo, so this script is injected
# into Node C's cloud-init: it builds the pinned CPU-only llama.cpp and serves the
# pinned `gemma` GGUF — Gemma 3 4B IT (Google open weights), the model the
# operator's decision step calls (image/models.lock.json).
#
# The weights are fetched from the project's OWN bucket (D3): a first boot must
# not pull a multi-GB weight from a public registry. The pinned object is served
# from storage.googleapis.com (the project bucket), not huggingface.co; the
# sha256 below is still checked, fail-closed, against the lock's digest.
#
# Frozen contract (docs/contracts/infra-decisions.md D2/D5, agent-runtime.md AR-4/AR-6):
#   * bind 0.0.0.0:8080 (the GCP firewall only allows 10.10.0.0/24 -> node-c:8080)
#   * OpenAI-compatible single-model deployment, served under the alias `gemma`
#   * CPU inference (this footprint has no GPU; `--n-gpu-layers 0` is implicit in a
#     GGML_CUDA=OFF build).
#
# Context/slot sizing: llama.cpp divides `--ctx-size` across `--parallel` slots,
# so the shipped flags give EVERY slot the full 16,384 tokens.
#
# It must be at least 2 slots. With `--parallel 1` this server is a single-slot
# queue: a request that arrives while another is being processed is held open
# until the first finishes. The AI operator's decision prompt is ~9,000 tokens
# (20-sample evidence window) and takes ~400 s to prefill on CPU, so while the
# operator was deciding — which the demo seeder's fault injection triggers
# continuously — a user's `POST /intents` extraction queued behind it and the
# request was never answered at all (`HTTP 000` after 200 s; the request never
# reached the controller's access log because the handler was still blocked
# inside the model call). Two slots is the minimum that makes the operator's
# decision and a user's extraction independent; measured on node-c, a warm
# extraction is ~5.5 s and the operator's decision ~400 s.
#
# `--ctx-size 32768 --parallel 2` = 2 × 16,384 tokens/slot, which keeps the
# 20-sample operator prompt (8,973 tokens measured 2026-10-12) inside one slot
# with headroom. (The older `--ctx-size 8192 --parallel 2` was rejected outright
# because that halves the per-slot window to 4,096.)
#
# `--reasoning-format none` keeps any inline reasoning block in the assistant
# `content`, so plain OpenAI clients (termini-agent, the ADK demo) always receive
# non-empty `content` and incremental SSE deltas (it is a no-op for Gemma).
#
# The double-underscore tokens below are substituted by Terraform (main.tf) at
# apply time so this file stays a plain, lintable shell script.
set -euo pipefail

LLAMACPP_COMMIT="__LLAMACPP_COMMIT__"
MODEL_URL="__MODEL_URL__"
MODEL_FILE="__MODEL_FILE__"
MODEL_SHA256="__MODEL_SHA256__"
MODEL_ALIAS="__MODEL_ALIAS__"
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

# Fail closed if the on-disk weights are not the pinned artifact.
got_sha="$(sha256sum "$MODEL_DIR/$MODEL_FILE" | awk '{print $1}')"
if [ "$got_sha" != "$MODEL_SHA256" ]; then
  echo "FATAL: $MODEL_FILE sha256 $got_sha != pinned $MODEL_SHA256"
  exit 1
fi
echo "$got_sha  $MODEL_FILE" > "$MODEL_DIR/$MODEL_FILE.sha256"

id termini-infer >/dev/null 2>&1 || \
  useradd --system --no-create-home --shell /usr/sbin/nologin termini-infer

cat > /etc/systemd/system/termini-llama-server.service <<UNIT
[Unit]
Description=Termini host-exposed llama.cpp inference server (Node C, :$PORT)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=termini-infer
ExecStart=$PREFIX/bin/llama-server --model $MODEL_DIR/$MODEL_FILE --alias $MODEL_ALIAS --host 0.0.0.0 --port $PORT --ctx-size 32768 --parallel 2 --threads 8 --reasoning-format none
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now termini-llama-server.service

echo "[$(date -u +%FT%TZ)] termini-llama-server enabled; health http://0.0.0.0:$PORT/health"
