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
# Node C is CPU-only. MEASURED on `n2-standard-8` (8 threads, `gemma` = Gemma 3 4B
# IT Q4_K_M): a decision over the operator's real prompt takes **minutes, not
# seconds** — ≈84 s with a one-sample evidence window and ≈6.7 min (402 s) with
# the real 20-sample window (prefill ≈33 tok/s, generation ≈8–10 tok/s). The real
# 20-sample request is 12,748 prompt tokens under Gemma's tokenizer and fits the
# 16,384-token slot.
#
# Node C MUST serve more than one slot (`--ctx-size 32768 --parallel 2` in
# `infra/gcp/cloud-init/llama-serve.sh`). With `--parallel 1` the server is a
# single-slot queue, so a `POST /intents` extraction that arrives while the
# operator is deciding waits behind the whole ~400 s decision and the request is
# never answered (HTTP 000) — the seeder-driven operator log and a responsive
# intent path were mutually exclusive. Two slots make them independent.
#
# So: this makes the architecture faithful (the model is on-prem and the calls
# reach Node C), but it does NOT make the decision step interactive. The
# interactive demo moment is carried by the deterministic fast-path
# (architecture.md R24). Budget `TERMINI_OPERATOR_DECISION_TIMEOUT_S` for the
# slow path. The operator loop holds NO DB transaction across the call
# (controller/app/operator/loop.py: detection commits before remediation), so
# ingest continues while a decision is in flight.
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
# The intent extractor shares that model but NOT that patience: it is a
# user-facing synchronous call inside POST /intents, so it gets its own bound.
# Without it the extraction inherited the 900 s decision timeout and a request
# that landed behind a decision hung until the client gave up (HTTP 000).
INTENT_TIMEOUT_S="${INTENT_TIMEOUT_S:-120}"

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
Environment=TERMINI_INFERENCE_MODEL=gemma
Environment=TERMINI_INFERENCE_TIMEOUT_S=${DECISION_TIMEOUT_S}
Environment=TERMINI_OPERATOR_DECISION_TIMEOUT_S=${DECISION_TIMEOUT_S}
Environment=TERMINI_INTENT_MODEL_TIMEOUT_S=${INTENT_TIMEOUT_S}
# The user-facing intent path gets its own bound (see INTENT_TIMEOUT_S above) AND
# its own model instance: even with a multi-slot server, the operator's ~12k-token
# decision prompt fills llama.cpp's batch, so a small concurrent extraction makes
# no progress until that prefill ends. Node C serves a dedicated small-context
# llama.cpp instance on :8090 for exactly this (infra/gcp/cloud-init/llama-serve.sh).
Environment=TERMINI_INTENT_INFERENCE_BASE_URL=http://${NODE_C}:8090
DROPIN

sudo systemctl daemon-reload
sudo systemctl restart termini-controller
sleep 5
echo "controller state: $(sudo systemctl is-active termini-controller)"
echo "--- effective inference/operator env ---"
sudo systemctl show termini-controller -p Environment | tr ' ' '\n' | grep -E 'INFERENCE|OPERATOR' || true
