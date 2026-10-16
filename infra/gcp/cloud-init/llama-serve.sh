#!/usr/bin/env bash
# termini-llama-serve.sh — first-boot inference node for Node C (task t_359ed606).
#
# The generic Termini node image stays activation-only (D3). Node C is the one
# node that must actually serve a model for the demo, so this script is injected
# into Node C's cloud-init: it builds the pinned CPU-only llama.cpp, serves the
# pinned `gemma` GGUF (Gemma 3 4B IT, Google open weights — the model the
# operator's decision step calls; image/models.lock.json), and puts the inference
# FRONT DOOR on :8080 as part of the SAME first boot.
#
# WHAT A FRESH NODE SERVES (explicit, task requirement 2)
#   The first boot installs `termini-inference-proxy.service` on 0.0.0.0:8080 and
#   moves llama.cpp to the LOOPBACK WORKER on 127.0.0.1:8081, with
#   TERMINI_INFERENCE_BACKEND = __INFERENCE_BACKEND__ — **`gemini-proxy` as
#   shipped**: :8080 is the AGENT-facing surface and it is answered by the
#   forwarded managed model (Google AI, through the proxy's `generateContent`
#   translation), labelled in-band with `backend` / `answered_by` / `served_model`
#   so a client can always tell where its tokens came from. This is the roll-back
#   of the inference split (D9) by kanban t_957956e5 / t_3fcee90b: the live-GPU
#   path is blocked by the L4 quota and this node's own CPU weights measured 402 s
#   on the operator's prompt, so the proxy is the path that works today. The LOCAL
#   backend — this node's own CPU weights, forwarding nothing, the on-prem
#   fallback — stays one env var / one command away on the running node:
#
#       termini-inference-switch local          # this node's own CPU weights
#       termini-inference-switch legacy         # the pre-proxy host-exposed server
#       termini-inference-switch status         # what is serving RIGHT NOW
#
#   Nothing on the node has to be reinstalled or rebuilt to move between them.
#
# WHO OWNS THE UNITS
#   The front door's units and env files are NOT written here. This script CALLS
#   infra/inference/install-inference-proxy.sh (staged by cloud-init at
#   $FRONT_DOOR_SRC, byte-identical to the committed file), which stays the SINGLE
#   SOURCE OF TRUTH for the worker unit, the proxy unit and their env files — so
#   the first-boot path and the manual path cannot drift.
#
# Frozen contract (docs/contracts/infra-decisions.md D2/D5, agent-runtime.md AR-4/AR-6):
#   * 0.0.0.0:8080 is the inference address (the GCP firewall only allows
#     10.10.0.0/24 -> node-c:8080); with the front door installed that address is
#     the PROXY's, and the worker behind it is loopback-only (AR-4's shape).
#   * OpenAI-compatible single-model deployment, served under the alias `gemma`
#   * CPU inference (this footprint has no GPU; `--n-gpu-layers 0` is implicit in a
#     GGML_CUDA=OFF build).
#
# Context/slot sizing: llama.cpp divides `--ctx-size` across `--parallel` slots,
# so the shipped worker flags (--ctx-size 32768 --parallel 2) give EVERY slot the
# full 16,384 tokens. See install-inference-proxy.sh for that unit.
#
# A SECOND instance is served, because one is not enough to keep the user-facing
# intent path alive:
#
#   * `termini-llama-intent` (:8090) — a small-context instance for
#     `POST /intents` extraction only. A second slot on the decision server is not
#     enough: llama.cpp fills its batch from the big prompt first, so a small
#     concurrent request gets no compute at all until the ~400 s prefill ends
#     (measured: the extraction timed out at its 120 s bound while the operator's
#     prompt processed). Separate processes give the extraction its own threads,
#     which is what makes a judge's keystroke answer while the operator log stays
#     live. The controller points at it with TERMINI_INTENT_INFERENCE_BASE_URL.
#     It is deliberately NOT fronted by the proxy (the front door speaks for the
#     frozen :8080 inference contract); fronting it too is an `agents` call.
#
# The thread budgets add to slightly more than the node's 8 vCPUs (6 + 3): the
# decision is the slow path and may yield.
#
# `--reasoning-format none` keeps any inline reasoning block in the assistant
# `content`, so plain OpenAI clients (termini-agent, the ADK demo) always receive
# non-empty `content` and incremental SSE deltas (it is a no-op for Gemma).
#
# The double-underscore tokens below are substituted by Terraform (main.tf) or by
# infra/gcp/cloud-init/node-c-serve-block.sh (the gcloud paths) at render time so
# this file stays a plain, lintable shell script.
set -euo pipefail

LLAMACPP_COMMIT="__LLAMACPP_COMMIT__"
MODEL_URL="__MODEL_URL__"
MODEL_FILE="__MODEL_FILE__"
MODEL_SHA256="__MODEL_SHA256__"
MODEL_ALIAS="__MODEL_ALIAS__"
PROXY_PORT="__PORT__"
BACKEND="__INFERENCE_BACKEND__"
INTENT_PORT="${INTENT_PORT:-8090}"

PREFIX="/opt/termini/llama.cpp"
BIN="$PREFIX/bin/llama-server"
MODEL_DIR="/var/lib/termini/models"
SRC="/usr/local/src/llama.cpp"
LOG="/var/log/termini-llama-serve.log"
# Where cloud-init staged the front-door payload (node-c-serve-block.sh writes it
# and defaults FRONT_DOOR_DIR to the same path).
FRONT_DOOR_SRC="${FRONT_DOOR_SRC:-/usr/local/src/termini-inference}"

mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
echo "[$(date -u +%FT%TZ)] termini-llama-serve: start (llama.cpp $LLAMACPP_COMMIT, backend=$BACKEND)"

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
install -m 0755 "$SRC/build/bin/llama-server" "$BIN"

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

# Dedicated small-context instance for `POST /intents` NL extraction. Separate
# PROCESS, so the operator's ~12k-token decision prefill cannot starve the
# extraction batch. The controller reaches it via
# TERMINI_INTENT_INFERENCE_BASE_URL=http://<node-c>:$INTENT_PORT.
cat > /etc/systemd/system/termini-llama-intent.service <<UNIT
[Unit]
Description=Termini intent-extraction llama.cpp server (Node C, :$INTENT_PORT)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=termini-infer
ExecStart=$BIN --model $MODEL_DIR/$MODEL_FILE --alias $MODEL_ALIAS --host 0.0.0.0 --port $INTENT_PORT --ctx-size 4096 --parallel 1 --threads 3 --reasoning-format none
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now termini-llama-intent.service

# --- the inference FRONT DOOR (single source of truth) ---------------------- #
# install-inference-proxy.sh writes BOTH units and both env files, enables them
# and waits for :8080 to answer:
#   * termini-llama-server.service -> loopback worker on 127.0.0.1:8081
#     (it RELEASES 0.0.0.0:$PROXY_PORT for the front door),
#   * termini-inference-proxy.service -> the front door on 0.0.0.0:$PROXY_PORT,
#   * /etc/termini/inference-proxy.env -> TERMINI_INFERENCE_BACKEND=$BACKEND,
#   * /usr/local/sbin/termini-inference-switch -> the one-step backend switch.
# Failing here is loud: cloud-init records the non-zero exit and the node comes up
# with no :8080 listener at all rather than a silently wrong one.
if [ ! -x "$FRONT_DOOR_SRC/install-inference-proxy.sh" ]; then
  echo "FATAL: $FRONT_DOOR_SRC/install-inference-proxy.sh missing — cloud-init did not"
  echo "       stage the front-door payload (see infra/gcp/cloud-init/node-c-serve-block.sh)"
  exit 1
fi

echo "[$(date -u +%FT%TZ)] installing the inference front door (backend=$BACKEND) via install-inference-proxy.sh"
PROXY_SRC="$FRONT_DOOR_SRC/proxy.py" \
UNIT_SRC="$FRONT_DOOR_SRC/termini-inference-proxy.service" \
SWITCH_SRC="$FRONT_DOOR_SRC/termini-inference-switch.sh" \
PREFIX="$PREFIX" \
BIN="$BIN" \
MODEL_DIR="$MODEL_DIR" \
MODEL_FILE="$MODEL_FILE" \
ALIAS="$MODEL_ALIAS" \
PROXY_PORT="$PROXY_PORT" \
  "$FRONT_DOOR_SRC/install-inference-proxy.sh" --backend "$BACKEND"

echo "[$(date -u +%FT%TZ)] inference front door installed: :$PROXY_PORT is termini-inference-proxy (backend $BACKEND)"
echo "[$(date -u +%FT%TZ)] terminus-llama-intent enabled (:${INTENT_PORT}): the POST /intents extraction endpoint"
curl -fsS -m 10 "http://127.0.0.1:$PROXY_PORT/health" || true
echo
