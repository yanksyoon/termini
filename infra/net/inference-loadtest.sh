#!/usr/bin/env bash
# inference-loadtest.sh — sustained streaming load test against the llama.cpp path.
#
# Part of kanban t_bcae0231. Drives repeated streaming chat-completions (the
# shape the agent runtime uses, docs/contracts/agent-runtime.md) for several
# minutes and reports first-token latency, inter-token jitter and — the thing
# that matters on stage — the number of dropped connections.
#
#   ./inference-loadtest.sh --path direct --duration 180 --prompt-bytes 65536 --out report.json
#   ./inference-loadtest.sh --url http://10.10.0.13:8080 --duration 60
#
# Exit 0 only when every request completed (zero dropped connections).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/path-env.sh
. "${HERE}/lib/path-env.sh"

PATH_NAME=""
URL=""
DURATION=180
CONCURRENCY=1
PROMPT_BYTES=65536
REQUEST_TIMEOUT=15
NETNS=""
OUT=""
PYTHON="${PYTHON:-python3}"

usage() {
  sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --path) PATH_NAME="${2:?}"; shift 2 ;;
    --url) URL="${2:?}"; shift 2 ;;
    --duration) DURATION="${2:?}"; shift 2 ;;
    --concurrency) CONCURRENCY="${2:?}"; shift 2 ;;
    --prompt-bytes) PROMPT_BYTES="${2:?}"; shift 2 ;;
    --request-timeout) REQUEST_TIMEOUT="${2:?}"; shift 2 ;;
    --netns) NETNS="${2:?}"; shift 2 ;;
    --out) OUT="${2:?}"; shift 2 ;;
    -h | --help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 1 ;;
  esac
done

if [ -n "$PATH_NAME" ]; then
  termini_path_valid "$PATH_NAME" || { echo "unknown path: $PATH_NAME (want direct|ovn)" >&2; exit 2; }
  if [ -z "$URL" ]; then
    host="$(termini_path_field "$PATH_NAME" host "10.10.0.13")"
    port="$(termini_path_field "$PATH_NAME" port "8080")"
    URL="http://${host}:${port}"
  fi
else
  [ -n "$URL" ] || URL="$(termini_inference_url)"
fi

# Normalize to the chat-completions endpoint and make the netns leg addressable.
case "$URL" in
  */v1/chat/completions) ENDPOINT="$URL" ;;
  *) ENDPOINT="${URL%/}/v1/chat/completions" ;;
esac

CLIENT="${HERE}/lab/stream_client.py"
[ -f "$CLIENT" ] || { echo "missing $CLIENT" >&2; exit 2; }

ARGS=(--url "$ENDPOINT" --duration "$DURATION" --concurrency "$CONCURRENCY"
  --prompt-bytes "$PROMPT_BYTES" --request-timeout "$REQUEST_TIMEOUT")
if [ -n "$OUT" ]; then
  ARGS+=(--out "$OUT")
fi

echo "== inference load test =="
echo "path      : ${PATH_NAME:-active}"
echo "endpoint  : ${ENDPOINT}"
echo "duration  : ${DURATION}s  concurrency=${CONCURRENCY}  prompt=${PROMPT_BYTES}B"

set +e
if [ -n "$NETNS" ]; then
  ip netns exec "$NETNS" "$PYTHON" "$CLIENT" "${ARGS[@]}"
  rc=$?
else
  "$PYTHON" "$CLIENT" "${ARGS[@]}"
  rc=$?
fi
set -e

if [ "$rc" -eq 0 ]; then
  echo "RESULT: PASS — zero dropped connections"
else
  echo "RESULT: FAIL — dropped connections (see report)"
fi
exit "$rc"
