#!/usr/bin/env bash
# [PRE-SPLIT / OPT-IN] Put the Termini operator into model mode, deciding on
# **Node C** (on-prem llama.cpp, CPU-only) over the underlay.
#
# >>> SUPERSEDED FOR THE SHIPPED DEPLOYMENT — READ BEFORE RUNNING <<<
# -----------------------------------------------------------------
# The inference surfaces were split (`docs/contracts/infra-decisions.md` **D9**,
# kanban `t_aa7a70f9`); that split was then rolled back for the agent surface
# (kanban **`t_957956e5` / `t_3fcee90b`):
#
#   * Node C's `:8080` is the **AGENT** surface and now **ships
#     `TERMINI_INFERENCE_BACKEND=gemini-proxy`** — the forwarded managed model,
#     labelled in-band (`backend`, `answered_by`, `served_model`,
#     `is_gpu_inference:false`). `local` (that node's own CPU weights, forwarding
#     nothing) is the one-variable fallback;
#   * the operator's **DIAGNOSIS** runs on the CONTROLLER, against the
#     provider-only listener on `127.0.0.1:8085` (`gemini-proxy` →
#     `aiplatform.googleapis.com`), installed by
#     `infra/inference/setup-inference-surfaces.sh`.
#
# This script does the OPPOSITE of D9: it points the diagnosis at Node C's agent
# surface. On the shipped footprint that means the diagnosis leaves the premises
# through **the node** rather than the controller's provider listener, and it is
# answered at whatever latency that node's backend has — provider-seconds in the
# shipped `gemini-proxy` mode, minutes in `local` mode (see LATENCY below). Routing
# the on-prem decision path through the node is a legitimate configuration to WANT,
# but it must be a deliberate choice, not a stale script run by hand: it now
# REFUSES to run unless you pass `--allow-unsplit`, and it disables the split's
# controller drop-in when it does (so the run is not silently half-applied).
#
# LATENCY — READ THIS BEFORE RUNNING IT
# -------------------------------------
# In `local` mode Node C is CPU-only. MEASURED on `n2-standard-8` (8 threads,
# `gemma` = Gemma 3 4B IT Q4_K_M): a decision over the operator's real prompt takes
# **minutes, not seconds** — ≈84 s with a one-sample evidence window and ≈6.7 min
# (402 s) with the real 20-sample window (prefill ≈33 tok/s, generation ≈8–10
# tok/s). The real 20-sample request is 12,748 prompt tokens under Gemma's
# tokenizer and fits the 16,384-token slot. This is one of the two reasons the
# shipped diagnosis path uses the controller's provider listener: it is the only
# surface that cannot be switched to the 402-second path.
#
# Node C MUST serve more than one slot (`--ctx-size 32768 --parallel 2` in
# `infra/gcp/cloud-init/llama-serve.sh`). With `--parallel 1` the server is a
# single-slot queue, so a `POST /intents` extraction that arrives while the
# operator is deciding waits behind the whole ~400 s decision and the request is
# never answered (HTTP 000). Two slots make them independent.
#
# The operator loop holds NO DB transaction across the call
# (controller/app/operator/loop.py: detection commits before remediation), so
# ingest continues while a decision is in flight.
#
# Usage (on the controller VM):
#   sudo bash setup-operator-node-c.sh --allow-unsplit [NODE_C_HOST]
#   NODE_C_HOST defaults to 10.10.0.13 (the frozen underlay address, D2).
set -euo pipefail

ALLOW_UNSPLIT=0
NODE_C=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --allow-unsplit) ALLOW_UNSPLIT=1 ;;
    -h|--help) sed -n '2,50p' "$0"; exit 0 ;;
    -*) echo "unknown argument: $1" >&2; exit 2 ;;
    *) [ -z "$NODE_C" ] || { echo "unexpected extra argument: $1" >&2; exit 2; }; NODE_C="$1" ;;
  esac
  shift
done
NODE_C="${NODE_C:-10.10.0.13}"
[ "$ALLOW_UNSPLIT" = "1" ] || {
  cat >&2 <<'REFUSE'
setup-operator-node-c.sh: REFUSING — this is the PRE-SPLIT wiring and it undoes
the inference split (infra-decisions.md D9, kanban t_aa7a70f9; the agent half of
that split was itself rolled back by kanban t_957956e5 / t_3fcee90b).

It would point the operator's DIAGNOSIS at Node C's :8080, which is the
AGENT-facing surface. Whether that is the CPU path depends on the node's backend:
shipped `gemini-proxy` forwards the diagnosis out through the node; only a node
explicitly switched to `local` answers it from CPU weights (≈402 s for the
20-sample prompt) and keeps it on the premises.

For the shipped wiring (diagnosis on the controller's provider-only listener),
run instead:

    sudo infra/inference/setup-inference-surfaces.sh

If routing the decision through Node C's front door is genuinely what you want,
re-run with --allow-unsplit; this script then also disables the split's
controller drop-in.
REFUSE
  exit 2
}

SPLIT_DROPIN="/etc/systemd/system/termini-controller.service.d/zz-inference-surfaces.conf"
if [ -f "$SPLIT_DROPIN" ]; then
  echo "!! --allow-unsplit: disabling the split's drop-in ($SPLIT_DROPIN -> .disabled)"
  sudo mv "$SPLIT_DROPIN" "$SPLIT_DROPIN.disabled"
fi

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
