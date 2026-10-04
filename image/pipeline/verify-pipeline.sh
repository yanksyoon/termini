#!/usr/bin/env bash
# verify-pipeline.sh — one command to verify the image pipeline on a clean
# checkout (no build host, no network, no multi-GB downloads).
#
#   ./verify-pipeline.sh                 # recipe check + local bake rehearsal
#   ./verify-pipeline.sh --write-examples
#
# Requires: bash, python3, jq. shellcheck is used when present (CI parity).
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REHEARSE_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --write-examples) REHEARSE_ARGS+=("--write-examples") ;;
    -h|--help) echo "usage: verify-pipeline.sh [--write-examples]"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

echo "==============================================================="
echo " 1/2  recipe check  (syntax, JSON, shellcheck, pin cross-check)"
echo "==============================================================="
bash "$SELF_DIR/build-image.sh" --check

echo
echo "==============================================================="
echo " 2/2  local bake rehearsal  (first-boot preflight, positive+negative)"
echo "==============================================================="
if [ "${#REHEARSE_ARGS[@]}" -gt 0 ]; then
  python3 "$SELF_DIR/tools/rehearse_bake.py" "${REHEARSE_ARGS[@]}"
else
  python3 "$SELF_DIR/tools/rehearse_bake.py"
fi

echo
echo "PIPELINE VERIFIED"
