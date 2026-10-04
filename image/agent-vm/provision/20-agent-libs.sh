#!/usr/bin/env bash
# 20-agent-libs.sh — install the pinned Python + Node agent libraries.
#
# Four isolated Python venvs (runtime + one per framework) because crewai/langchain/
# autogen pin conflicting versions of openai/opentelemetry/pydantic; isolating them
# keeps `pip check` clean in every environment. All wheels come from the hashed locks
# (--require-hashes), so the bake is byte-reproducible and boot installs nothing.
set -euo pipefail

BUILD_CTX="${TERMINI_BUILD_CTX:-/opt/termini/build}"
REQS_DIR="${BUILD_CTX}/requirements"
VENVS_DIR="/opt/termini/venvs"
NODE_DIR="/opt/termini/node"

install_lock() { # venv_dir lock_file label
  local venv="$1" lock="$2" label="$3"
  echo "[provision:20-agent-libs] installing $label -> $venv"
  "$venv/bin/pip" install --quiet --no-cache-dir --upgrade pip
  "$venv/bin/pip" install --quiet --no-cache-dir --require-hashes -r "$lock"
  "$venv/bin/pip" check
}

install_lock /opt/termini/venv "$REQS_DIR/runtime.lock" "runtime"
install_lock "$VENVS_DIR/langchain" "$REQS_DIR/langchain.lock" "langchain"
install_lock "$VENVS_DIR/crewai" "$REQS_DIR/crewai.lock" "crewai"
install_lock "$VENVS_DIR/autogen" "$REQS_DIR/autogen.lock" "autogen"

# --- Node libraries (openai, pino, undici) from the committed lock ------------
NODE_APP="/opt/termini/nodejs"
install -d -m 0755 "$NODE_APP"
cp "${BUILD_CTX}/node/package.json" "${BUILD_CTX}/node/package-lock.json" "$NODE_APP/"
( cd "$NODE_APP" && PATH="$NODE_DIR/bin:$PATH" "$NODE_DIR/bin/npm" ci --omit=dev --no-audit --no-fund )
PATH="$NODE_DIR/bin:$PATH" "$NODE_DIR/bin/node" -e 'require("/opt/termini/nodejs/node_modules/openai"); require("/opt/termini/nodejs/node_modules/pino"); require("/opt/termini/nodejs/node_modules/undici"); console.log("node libs ok")'

echo "[provision:20-agent-libs] python + node agent libraries baked"
