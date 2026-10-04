#!/usr/bin/env bash
# verify-gpu.sh — Node C GPU acceptance for kanban t_4ccb8ea7.
#
#   ./verify-gpu.sh              # on the live Node C: nvidia-smi + a host-process
#                                # CUDA probe; writes the JSON report
#   ./verify-gpu.sh --rehearse   # offline: exercise the verifier against stub
#                                # fixtures (no GPU required)
#
# Acceptance it checks: nvidia-smi reports the Node C GPU and the device is usable
# from the host process (open /dev/nvidia* + a real libcudart call).
#
# Diagnostic only: node-bootstrap.md §9 keeps a driverless node registering, so
# nothing gates first boot on this.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${TERMINI_LOG_DIR:-/var/log/termini}"
JSON="$LOG_DIR/gpu-check.json"
MODE="live"
PASSTHRU=()

usage() {
  cat <<'USAGE'
usage: verify-gpu.sh [--rehearse] [--json PATH] [--skip-cudart]
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --rehearse) MODE="rehearse"; shift ;;
    --json) JSON="$2"; shift 2 ;;
    --skip-cudart) PASSTHRU+=(--skip-cudart); shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ "$MODE" = "rehearse" ]; then
  exec python3 "$SELF_DIR/tools/rehearse_gpu.py" "${PASSTHRU[@]}"
fi

command -v python3 >/dev/null 2>&1 || { echo "FATAL: python3 is required" >&2; exit 3; }
if ! mkdir -p "$(dirname "$JSON")" 2>/dev/null; then
  JSON="$(mktemp -t gpu-check.XXXXXX.json)"
  echo "[verify-gpu] note: log dir not writable, using $JSON"
fi

echo "== Node C GPU check: nvidia-smi + host-process CUDA probe"
if python3 "$SELF_DIR/tools/gpu_check.py" --json "$JSON" "${PASSTHRU[@]}"; then
  echo "RESULT: PASS  (report: $JSON)"
  exit 0
fi
echo "RESULT: FAIL  (report: $JSON)"
exit 1
