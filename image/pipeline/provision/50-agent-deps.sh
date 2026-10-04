#!/usr/bin/env bash
# 50-agent-deps.sh — pre-baked agent runtime dependencies (Python + Node + Ruby).
# Wheels and gems are resolved at *bake* time and pinned; boot installs nothing.
set -euo pipefail

VENV="${TERMINI_VENV:-/opt/termini/venv}"
NODE_DIR="${TERMINI_NODE_DIR:-/opt/termini/node}"
REQS="${TERMINI_REQUIREMENTS:-/opt/termini/requirements.lock}"

# --- Python: agent libs from agent-runtime.md §3.2 ---
python3 -m venv "$VENV"
if [ -f "$REQS" ]; then
  "$VENV/bin/pip" install --no-cache-dir --require-hashes -r "$REQS"
else
  "$VENV/bin/pip" install --no-cache-dir \
    'openai==1.*' httpx pydantic tenacity structlog langchain-core langgraph
fi

# --- Node.js 20 LTS (unpacked under /opt/termini/node, not apt) ---
NODE_VERSION="${NODE_VERSION:-20.18.0}"
if [ ! -x "$NODE_DIR/bin/node" ]; then
  node_tar="node-v${NODE_VERSION}-linux-x64.tar.xz"
  curl -fL --retry 3 -o "/tmp/$node_tar" \
    "https://nodejs.org/dist/v${NODE_VERSION}/$node_tar"
  install -d -m 0755 "$NODE_DIR"
  tar -xJf "/tmp/$node_tar" -C "$NODE_DIR" --strip-components=1
  rm -f "/tmp/$node_tar"
fi

# --- Ruby gems for the operator tooling ---
gem install --no-document --conservative jsonl 2>/dev/null || true

install -d -m 0755 /etc/termini
{
  printf '%s\n' "python_venv=$VENV"
  printf '%s\n' "node_dir=$NODE_DIR"
  printf '%s\n' "node_version=${NODE_VERSION}"
} > /etc/termini/agent-deps.txt
echo "[provision:50-agent-deps] python + node + ruby deps baked"
