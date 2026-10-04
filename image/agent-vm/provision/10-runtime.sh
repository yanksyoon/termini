#!/usr/bin/env bash
# 10-runtime.sh — Python 3.12 + Node.js 20 runtimes (agent-runtime.md §3.2).
#
# Python: the interpreter ships with the OS; each agent environment gets its own
# venv so frameworks with conflicting pins cannot break the runtime venv.
# Node: unpacked from the pinned upstream tarball into /opt/termini/node (no apt).
set -euo pipefail

VERSION="${TERMINI_VERSION:-2026.10.0}"
VENVS_DIR="/opt/termini/venvs"

# --- Node.js (pinned) --------------------------------------------------------
NODE_VERSION="${NODE_VERSION:-20.20.2}"
NODE_SHA256="${NODE_SHA256:-df770b2a6f130ed8627c9782c988fda9669fa23898329a61a871e32f965e007d}"
NODE_DIR="/opt/termini/node"
node_tar="node-v${NODE_VERSION}-linux-x64.tar.xz"
node_url="https://nodejs.org/dist/v${NODE_VERSION}/${node_tar}"

if [ ! -x "$NODE_DIR/bin/node" ]; then
  tmp="$(mktemp -d)"
  curl -fsSL --retry 3 -o "$tmp/$node_tar" "$node_url"
  echo "${NODE_SHA256}  ${tmp}/${node_tar}" | sha256sum -c -
  install -d -m 0755 "$NODE_DIR"
  tar -xJf "$tmp/$node_tar" -C "$NODE_DIR" --strip-components=1
  rm -rf "$tmp"
fi
"$NODE_DIR/bin/node" --version

# expose node/npm/npx on the default PATH (agent code + scripts expect `node`).
for b in node npm npx; do
  [ -e "$NODE_DIR/bin/$b" ] && ln -sf "$NODE_DIR/bin/$b" "/usr/local/bin/$b"
done

# --- Python venvs (empty; 20-agent-libs.sh populates them) --------------------
install -d -m 0755 "$VENVS_DIR" /opt/termini
python3 -m venv /opt/termini/venv
for fw in langchain crewai autogen; do
  python3 -m venv "$VENVS_DIR/$fw"
done

install -d -m 0755 /etc/termini
{
  printf 'python=%s\n' "$(python3 --version)"
  printf 'runtime_venv=/opt/termini/venv\n'
  printf 'framework_venvs=%s/{langchain,crewai,autogen}\n' "$VENVS_DIR"
  printf 'node_dir=%s\n' "$NODE_DIR"
  printf 'node_version=%s\n' "$NODE_VERSION"
  printf 'image_version=%s\n' "$VERSION"
} > /etc/termini/runtime.txt

echo "[provision:10-runtime] python + node runtimes baked"
