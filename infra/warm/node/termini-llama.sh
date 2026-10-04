#!/usr/bin/env bash
# termini-llama — start the resident llama.cpp OpenAI-compatible server.
#
# Runs on the inference node. The GGUF is read from local disk with
# --load-mode none, i.e. the whole model is read into RAM at start and stays
# resident; the demo never waits for a cold load. No network access is used.
set -euo pipefail
. /etc/termini/model.env

model_path="${MODEL_DIR}/${MODEL_FILE}"
[ -f "$model_path" ] || { echo "model not present: $model_path" >&2; exit 1; }

exec /opt/termini/llama.cpp/bin/llama-server \
  --host "${LLAMA_HOST:-0.0.0.0}" \
  --port "${INFERENCE_PORT:-8080}" \
  --model "$model_path" \
  --alias "${MODEL_ALIAS}" \
  --n-gpu-layers "${N_GPU_LAYERS:-0}" \
  --ctx-size "${CTX_SIZE:-4096}" \
  --parallel "${PARALLEL:-2}" \
  --load-mode "${LOAD_MODE:-none}"
