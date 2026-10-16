#!/usr/bin/env bash
# Evidence harness for the dashboard's metric-expansion panel (task t_8b60acb2).
#
# WHAT IS REAL HERE
#   * a real `python3 -m app.main` controller process with its own temp DB;
#   * a real node registration (single-use bootstrap token) and the REAL sample
#     the collector card recorded — `docs/evidence/2026-10-16-metric-expansion/
#     node-payload.json`, copied to ../sample/ — pushed through the real
#     governance gate (C2 scan + C1 transform check + redactor version), the real
#     schema validation and the real store, via `POST /v1/nodes/{id}/telemetry`;
#   * the real dashboard bundle served on a second origin and driven by a real
#     Chromium (Xvfb + CDP). The project credential is seeded into
#     `sessionStorage`; it is never placed in a URL;
#   * the panel text is read OUT OF THE DOM (`innerText`) and the PNGs are the
#     browser's own output — nothing in capture/ is retyped by hand.
#
# WHAT IS DERIVED (stated, not hidden)
#   * the SECOND sample is derived from the first by removing every §4.12–§4.14
#     field (`tools/post_real_payload.py --stage absent` writes it to
#     capture/sample-no-expansion-fields.json). It is the shape a node emits when
#     its sources are unreadable, and it is NOT a recorded node capture — the
#     file and the INDEX say so.
#
# Usage: run-capture.sh <out-dir> [work-dir]
set -euo pipefail

OUT_ARG="${1:?usage: run-capture.sh <out-dir> [work-dir]}"
mkdir -p "$OUT_ARG"
OUT="$(cd "$OUT_ARG" && pwd)"
TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$TOOLS/../../../.." && pwd)"
WORK="${2:-$(mktemp -d "${TMPDIR:-/tmp}/termini-metric-panel.XXXXXX")}"
CAP="$OUT/capture"
mkdir -p "$CAP" "$WORK"

PROJECT_ID="01J0DEM0WARM000000000000AB"
PROJECT_TOKEN="evidence-project-token"
BOOTSTRAP_TOKEN="ev-metric-expansion-1"
VM_ID="01M539DJ5WDFPAH3NR9NNXXVS2"
CDP_PORT="${CDP_PORT:-9222}"
SCREEN_W="${SCREEN_W:-1440}"
SCREEN_H="${SCREEN_H:-900}"

free_port() {
  python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}
CPORT="$(free_port)"; DPORT="$(free_port)"
BASE="http://127.0.0.1:$CPORT"
DASH="http://127.0.0.1:$DPORT"

CONTROLLER_PID=""; DASH_PID=""
RUNDIR="$WORK/browser"          # Xvfb + Chromium run dir (pid files live here)
cleanup() {
  for f in "$RUNDIR/chrome.pid" "$RUNDIR/xvfb.pid"; do
    if [ -f "$f" ]; then kill "$(cat "$f")" 2>/dev/null || true; fi
  done
  [ -n "$DASH_PID" ] && kill "$DASH_PID" 2>/dev/null || true
  [ -n "$CONTROLLER_PID" ] && kill "$CONTROLLER_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

say() { printf '\n=== %s ===\n' "$*"; }

start_controller() {
  # `exec` in the backgrounded subshell so `$!` IS the controller's pid.
  ( cd "$REPO/controller" && exec env \
      TERMINI_HOST=127.0.0.1 TERMINI_PORT="$CPORT" TERMINI_DB_PATH="$WORK/controller.db" \
      TERMINI_BOOTSTRAP_TOKEN="$BOOTSTRAP_TOKEN" TERMINI_PROJECT_ID="$PROJECT_ID" \
      TERMINI_PROJECT_TOKEN="$PROJECT_TOKEN" TERMINI_MIN_REDACTOR_VERSION=3 \
      TERMINI_OPERATOR_ENABLED=false PYTHONUNBUFFERED=1 \
      python3 -m app.main ) >"$WORK/controller.log" 2>&1 &
  CONTROLLER_PID=$!
  for _ in $(seq 1 100); do
    if ! kill -0 "$CONTROLLER_PID" 2>/dev/null; then
      echo "controller exited during startup"; sed 's/^/  | /' "$WORK/controller.log" | tail -20; return 1
    fi
    code="$(curl -s -o /dev/null -w '%{http_code}' -m 2 \
      -H "Authorization: Bearer $PROJECT_TOKEN" "$BASE/v1/projects/$PROJECT_ID/nodes" || true)"
    [ "$code" = "200" ] && return 0
    sleep 0.3
  done
  echo "controller did not come up"; sed 's/^/  | /' "$WORK/controller.log" | tail -20; return 1
}

############ 1. controller ####################################################
say "1. controller on $BASE (temp DB: $WORK/controller.db)"
start_controller

############ 2. the REAL sample, through the real ingest path #################
say "2. register a node and push the committed real sample (governance + schema + store)"
python3 "$TOOLS/post_real_payload.py" \
  --base "$BASE" --bootstrap-token "$BOOTSTRAP_TOKEN" --project-id "$PROJECT_ID" \
  --project-token "$PROJECT_TOKEN" --vm-id "$VM_ID" --stage real \
  --payload "$OUT/sample/node-payload.json" --node-out "$WORK/node.json" \
  --capture-dir "$CAP" | tee "$CAP/01-real-sample-ingest-summary.json"

############ 3. dashboard + browser ###########################################
say "3. dashboard on $DASH + Chromium (Xvfb, CDP :$CDP_PORT)"
( cd "$REPO" && exec python3 -m http.server "$DPORT" --bind 127.0.0.1 --directory dashboard ) \
  >"$WORK/dashboard.log" 2>&1 &
DASH_PID=$!
for _ in $(seq 1 40); do curl -s -o /dev/null "$DASH/index.html" && break; sleep 0.25; done

DASH_URL="$DASH/?apiBase=$BASE/v1&projectId=$PROJECT_ID"
case "$DASH_URL" in *token=*) echo "REFUSING: the dashboard URL carries a credential"; exit 1;; esac

CHROME="${CHROME:-/home/ubuntu/.hermes/tools/chromium-1208/chrome-linux64/chrome}"
# A stale browser on the CDP port would be driven instead of this run's page and
# the capture would show the WRONG tab (observed on the first attempt: a leaked
# Chromium from the previous run answered :9222 with its old page and the capture
# came back empty). Refuse rather than produce a plausible-looking wrong capture.
if curl -s -m 2 "http://127.0.0.1:$CDP_PORT/json/version" >/dev/null 2>&1; then
  echo "REFUSING: something is already answering CDP on :$CDP_PORT (stale browser? kill it first)"; exit 1
fi
CHROME="$CHROME" RUNDIR="$RUNDIR" SCREEN_W="$SCREEN_W" SCREEN_H="$SCREEN_H" PORT="$CDP_PORT" \
  bash "$TOOLS/start-browser.sh" "$DASH_URL"

capture() { # capture <prefix> [--expect X --forbid Y ...]
  local prefix="$1"; shift
  python3 "$TOOLS/capture_panel.py" --port "$CDP_PORT" --api-base "$BASE/v1" \
    --project-id "$PROJECT_ID" --project-token "$PROJECT_TOKEN" \
    --capture-dir "$CAP" --prefix "$prefix" "$@"
}

############ 4. the panel, fed by the real sample #############################
say "4. capture: the panel rendering the REAL sample"
capture 01-real-sample \
  --expect "PSI pressure (latest sample)" \
  --expect "OOM kills" \
  --expect "30,730" \
  --expect "TCP retransmits" \
  --expect "cumulative" \
  --expect "conntrack table used" \
  --forbid "not reported"

# The X11 grab is the same frame the CDP shot sees, but with the browser chrome in
# it: the URL is evidence too. Optional — a host without ffmpeg still gets the PNGs.
FFMPEG="$(command -v ffmpeg || echo /home/ubuntu/.hermes/tools/ffmpeg-9.0.1-linux-x64/bin/ffmpeg)"
if [ -x "$FFMPEG" ]; then
  DISPLAY="${DISPLAY_NUM:-:99}" "$FFMPEG" -y -loglevel error -f x11grab \
    -video_size "${SCREEN_W}x${SCREEN_H}" -i "${DISPLAY_NUM:-:99}" -frames:v 1 \
    "$CAP/01-real-sample-window.png" || true
fi

############ 5. absent ≠ zero ################################################
say "5. the same node with every §4.12–§4.14 source unreadable (fields dropped)"
python3 "$TOOLS/post_real_payload.py" \
  --base "$BASE" --project-id "$PROJECT_ID" --project-token "$PROJECT_TOKEN" \
  --stage absent --node-in "$WORK/node.json" \
  --payload "$OUT/sample/node-payload.json" --node-out "$WORK/node.json" \
  --capture-dir "$CAP" | tee "$CAP/02-absent-not-zero-ingest-summary.json"

capture 02-absent-not-zero \
  --expect "not reported" \
  --forbid "OOM kills"

if [ -x "$FFMPEG" ]; then
  DISPLAY="${DISPLAY_NUM:-:99}" "$FFMPEG" -y -loglevel error -f x11grab \
    -video_size "${SCREEN_W}x${SCREEN_H}" -i "${DISPLAY_NUM:-:99}" -frames:v 1 \
    "$CAP/02-absent-not-zero-window.png" || true
fi

say "done"
echo "captures : $CAP"
echo "work dir : $WORK"
