#!/usr/bin/env bash
# 30-llamacpp.sh — build the pinned llama.cpp and install llama-server/llama-cli.
# GPU variant builds with GGML_CUDA=ON; compute variant builds CPU (used for the
# offline-load rehearsal and as a fallback). Nothing is built at boot.
set -euo pipefail

LLAMACPP_COMMIT="${LLAMACPP_COMMIT:-7fe450e19305b828c199d602c23a8337aaa1f03b}"
VARIANT="${TERMINI_VARIANT:-compute}"
SRC="${LLAMACPP_SRC:-/usr/local/src/llama.cpp}"
PREFIX="${LLAMACPP_PREFIX:-/opt/termini/llama.cpp}"
JOBS="${LLAMACPP_JOBS:-$(nproc)}"

if [ ! -d "$SRC/.git" ]; then
  git clone --quiet https://github.com/ggml-org/llama.cpp "$SRC"
fi

git -C "$SRC" fetch --quiet --depth 1 origin "$LLAMACPP_COMMIT"
git -C "$SRC" checkout --quiet "$LLAMACPP_COMMIT"
built_commit="$(git -C "$SRC" rev-parse HEAD)"
if [ "$built_commit" != "$LLAMACPP_COMMIT" ]; then
  echo "FATAL: llama.cpp checkout $built_commit != pinned $LLAMACPP_COMMIT" >&2
  exit 1
fi

cuda_flag=OFF
if [ "$VARIANT" = "gpu" ]; then
  cuda_flag=ON
fi

cmake -S "$SRC" -B "$SRC/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_NATIVE=OFF \
  -DGGML_CUDA="$cuda_flag" \
  -DBUILD_SHARED_LIBS=OFF \
  -DLLAMA_CURL=OFF
cmake --build "$SRC/build" --config Release -j "$JOBS" --target llama-server llama-cli

install -d -m 0755 "$PREFIX/bin"
install -m 0755 "$SRC/build/bin/llama-server" "$PREFIX/bin/llama-server"
install -m 0755 "$SRC/build/bin/llama-cli" "$PREFIX/bin/llama-cli"

# Static build ⇒ self-contained binaries. Fail the bake loudly if that ever
# regresses (a shared build would need .so companions + LD_LIBRARY_PATH, which
# would not be installed here).
if ! "$PREFIX/bin/llama-server" --version >/dev/null 2>&1; then
  echo "FATAL: llama-server is not self-contained (missing shared libs?)" >&2
  ldd "$PREFIX/bin/llama-server" >&2 || true
  exit 1
fi

install -d -m 0755 /etc/termini
{
  printf '%s\n' "commit=${built_commit}"
  printf '%s\n' "variant=${VARIANT}"
  printf '%s\n' "cuda=${cuda_flag}"
  printf '%s\n' "prefix=${PREFIX}"
} > /etc/termini/llama.cpp-build.txt

# systemd template: one worker per model alias (agent-runtime.md §8.1). The
# router (termini-infer-router) is owned by the Node C inference task and fronts
# these workers; this unit only defines how a worker is started.
cat > /etc/systemd/system/llama-cpp@.service <<'UNIT'
[Unit]
Description=Termini llama.cpp worker (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=termini-infer
EnvironmentFile=-/etc/termini/llama-cpp/%i.env
ExecStart=/opt/termini/llama.cpp/bin/llama-server \
  --model ${MODEL_PATH} \
  --host 127.0.0.1 --port ${PORT} \
  --n-gpu-layers ${N_GPU_LAYERS} --ctx-size ${CTX} --parallel ${PARALLEL} \
  --flash-attn
Restart=on-failure
RestartSec=3
# No network fetch: the model file must already be on local disk.
ProtectSystem=strict
ReadOnlyPaths=/var/lib/termini/models
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

[Install]
WantedBy=multi-user.target
UNIT

echo "[provision:30-llamacpp] built ${built_commit} variant=${VARIANT} cuda=${cuda_flag}"
