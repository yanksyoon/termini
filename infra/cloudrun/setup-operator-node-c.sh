#!/usr/bin/env bash
# Put the Termini operator into model mode, deciding on **Node C** (on-prem
# llama.cpp, CPU-only) over the underlay — the on-prem counterpart of
# `setup-operator-model.sh` (which points the same seam at the managed Cloud Run
# GPU model).
#
# WHY THIS EXISTS
# ---------------
# The requirement is that Node C — the CPU-only inference node the footprint pays
# for — actually hosts the model the operator's decision step calls, instead of
# that call leaving the premises. This script is that wiring. It changes exactly
# one thing: the decision adapter's base URL, from the loopback inference proxy
# (`:8090`, Cloud Run) to Node C's llama.cpp service on the underlay.
#
# The decision adapter builds `chat_url = TERMINI_INFERENCE_BASE_URL +
# /v1/chat/completions`, so pointing the base URL at Node C is the whole change;
# no controller or adapter code differs between the two destinations, and the
# `externalModelCall()` boundary guard (governance.md §7.2/§9.3) applies to both.
#
# LATENCY — READ THIS BEFORE RUNNING IT
# -------------------------------------
# Node C is CPU-only. MEASURED on `n2-standard-8` (8 threads,
# `llama.cpp --ctx-size 16384`, `deepseek-flash` = DeepSeek-R1-Distill-Llama-8B
# Q4_K_M): a decision over the operator's real prompt takes **minutes, not
# seconds** — ≈156 s with a one-sample evidence window and ≈9–10 min with the
# real 20-sample window (prefill ≈15.7 tok/s, generation ≈4.1 tok/s). The real
# 20-sample request is 8,973 prompt tokens and is REJECTED outright by the
# shipped server context (`--ctx-size 8192 --parallel 2` ⇒ 4,096 tokens/slot).
#
# So: this makes the architecture faithful (the model is on-prem and the calls
# reach Node C), but it does NOT make the decision step interactive. The
# interactive demo moment is carried by the deterministic fast-path
# (architecture.md R24). Budget `TERMINI_OPERATOR_DECISION_TIMEOUT_S` for the
# slow path, and be aware the operator loop holds its DB transaction for the
# duration of the call, so ingest stalls while a decision is in flight.
#
# Usage (on the controller VM):
#   sudo bash setup-operator-node-c.sh [NODE_C_HOST]
#   NODE_C_HOST defaults to 10.10.0.13 (the frozen underlay address, D2).
set -euo pipefail

NODE_C="${1:-10.10.0.13}"
BASE_URL="http://${NODE_C}:8080"
# A decision can legitimately take ~10 min on CPU; the default 30 s would make
# every decision a timeout+fallback. Widen it on purpose.
DECISION_TIMEOUT_S="${DECISION_TIMEOUT_S:-900}"

echo "--- proving Node C's llama.cpp is reachable before touching the controller ---"
if ! curl -sS -m 10 -o /dev/null -w '  /health http=%{http_code}\n' "${BASE_URL}/health"; then
  echo "FATAL: ${BASE_URL}/health unreachable (is Node C up and is the firewall rule in place?)" >&2
  exit 1
fi
curl -sS -m 10 "${BASE_URL}/v1/models" | head -c 200; echo

echo "--- flipping the operator to model mode against Node C ---"
sudo mkdir -p /etc/systemd/system/termini-controller.service.d
sudo tee /etc/systemd/system/termini-controller.service.d/zz-node-c-model.conf >/dev/null <<DROPIN
[Service]
# Node C hosts the model. Overrides the Cloud Run proxy base URL set by
# setup-operator-model.sh (drop-in ordering: 'zz-' sorts last, so it wins).
Environment=TERMINI_OPERATOR_ENABLED=true
Environment=TERMINI_OPERATOR_DECISION_MODE=model
Environment=TERMINI_INFERENCE_BASE_URL=${BASE_URL}
Environment=TERMINI_INFERENCE_MODEL=deepseek-flash
Environment=TERMINI_INFERENCE_TIMEOUT_S=${DECISION_TIMEOUT_S}
Environment=TERMINI_OPERATOR_DECISION_TIMEOUT_S=${DECISION_TIMEOUT_S}
DROPIN

sudo systemctl daemon-reload
sudo systemctl restart termini-controller
sleep 5
echo "controller state: $(sudo systemctl is-active termini-controller)"
echo "--- effective inference/operator env ---"
sudo systemctl show termini-controller -p Environment | tr ' ' '\n' | grep -E 'INFERENCE|OPERATOR' || true
