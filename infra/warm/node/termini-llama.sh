#!/usr/bin/env bash
# termini-llama — start the resident llama.cpp OpenAI-compatible server.
#
# Runs on the inference node. The GGUF is read from local disk with
# --load-mode none, i.e. the whole model is read into RAM at start and stays
# resident; the demo never waits for a cold load. No network access is used.
#
# Serve flags mirror the FROZEN Node C endpoint contract (infra/inference/
# activate-endpoint.sh, infra/gcp/cloud-init/llama-serve.sh): --ctx-size 8192,
# --parallel 2, --reasoning-format none. `--reasoning-format none` keeps the
# R1-distill think block inlined in delta.content rather than on a separate
# delta.reasoning_content channel, so the reasoning trace cannot starve the
# answer out of a small max_tokens budget. Only `--n-gpu-layers 0` is
# lab-specific (this host has no GPU).
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
  --ctx-size "${CTX_SIZE:-8192}" \
  --parallel "${PARALLEL:-2}" \
  --reasoning-format none \
  --load-mode "${LOAD_MODE:-none}"
