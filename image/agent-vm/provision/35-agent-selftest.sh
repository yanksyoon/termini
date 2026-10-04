#!/usr/bin/env bash
# 35-agent-selftest.sh — quality gate: run the termini-agent unit tests at bake time.
# If the runtime does not pass its own tests against the baked deps, the bake fails.
set -euo pipefail

BUILD_CTX="${TERMINI_BUILD_CTX:-/opt/termini/build}"
RUNTIME_VENV="/opt/termini/venv"

echo "[provision:35-agent-selftest] running termini-agent unit tests"
( cd "$BUILD_CTX/agent" && "$RUNTIME_VENV/bin/python" -m unittest discover -s tests -t . -v )

echo "[provision:35-agent-selftest] unit tests passed"
