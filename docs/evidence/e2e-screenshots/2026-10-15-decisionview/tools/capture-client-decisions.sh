#!/usr/bin/env bash
# Re-runnable capture: the client dashboard's live decision view, against the
# PUBLISHED deployment (Firebase Hosting) and the standing controller.
#
# Produces, for the run-id this file lives under:
#   client/01-model-decision.png        (acceptance a)
#   client/02-fallback-decision.png     (acceptance b)
#   client/03-log-panel-events.png      (acceptance c)
#   client/04-decider-label.png         (acceptance d)
#   client/05-audit-detail-dialog.png   (supporting a: the audit id in full)
# each with a .txt sidecar carrying the raw API/stream output behind the pixels,
# plus client/verification.txt with the DOM facts read back after each shot.
#
# How it captures: the convention wants the browser chrome in the image (visible
# URL). Headless CDP screenshots cannot do that, so we drive a HEADFUL Chromium
# inside Xvfb over a stdlib CDP client and grab the X root window with ffmpeg
# x11grab. Nothing is cropped, annotated, or composited.
#
# Prereqs on this host: Xvfb, xdotool, xdpyinfo, ffmpeg, the bundled Chromium.
set -euo pipefail

HOSTING="${HOSTING:-https://termini-510609.web.app}"
API="${API:-https://34-93-190-64.sslip.io/v1}"
PROJECT="${PROJECT:-01J0DEM0WARM000000000000AB}"
# The published demo project token (it is in the documented dashboard URL and in
# the README). No other credential is used or written anywhere in this run.
DEMO_TOKEN="${DEMO_TOKEN:-demo-project-token}"

BASE_QS="apiBase=${API}&projectId=${PROJECT}&token=${DEMO_TOKEN}"
URL_PLAIN="${HOSTING}/?${BASE_QS}"
# The decision rows currently on the fleet come from the demo (seeder) nodes;
# include=synthetic makes the panel LABEL them "(demo)" instead of implying real
# hardware. Plain link for the log panel + decider label, which are about the
# live stream and the egress record, not about which node fired.
URL_SYNTH="${HOSTING}/?${BASE_QS}&synthetic=1&decisionLimit=200"

EVID="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$EVID/client"
TOOLS="$EVID/tools"
TMP=/tmp/termini-capture
PORT=9222
FFMPEG="${FFMPEG:-/home/ubuntu/.hermes/tools/ffmpeg-9.0.1-linux-x64/bin/ffmpeg}"
W=1280
H=900
mkdir -p "$OUT" "$TMP"
: > "$OUT/verification.txt"

log()  { printf '%s\n' "$*" | tee -a "$OUT/verification.txt"; }
now()  { date -u +%Y-%m-%dT%H:%M:%SZ; }
js()   { cdp_retry "$1" json; }
jst()  { cdp_retry "$1" text; }
# The renderer can be busy (200 decision rows + a live log tail re-rendering
# while the SSE pumps) and a single evaluate then times out. Retry instead of
# failing the run: a capture harness that dies on a 60s hiccup is not re-runnable.
cdp_retry() {
  local expr="$1" mode="$2" out
  for _ in 1 2 3 4; do
    if [ "$mode" = "text" ]; then
      out="$(python3 "$TOOLS/cdp_eval.py" "$PORT" --text "$expr" 2>/dev/null)" || true
    else
      out="$(python3 "$TOOLS/cdp_eval.py" "$PORT" "$expr" 2>/dev/null)" || true
    fi
    if [ -n "$out" ]; then printf '%s\n' "$out"; return 0; fi
    sleep 3
  done
  printf '%s\n' '{"error":"cdp evaluate failed after 4 attempts"}'
  return 1
}
close_dialogs() { js '(() => {const d = document.querySelector("dialog[open]"); if (d) d.close(); return "dialogs closed"})()' >/dev/null; }
shot() { "$FFMPEG" -hide_banner -loglevel error -y -f x11grab -video_size "${W}x${H}" \
           -i :99 -frames:v 1 "$OUT/$1"; log "  shot $1 -> $(stat -c%s "$OUT/$1") bytes"; }
# scroll so the given selector's top sits `off` px below the viewport top.
# Default 70: the dashboard's own header bar is sticky (it overlays the first
# ~57px of the scrolled page), so an element parked at y=8 would be hidden
# behind it and the screenshot would look like the panel has no heading.
scroll_to() { js "window.scrollTo(0, Math.max(0, document.querySelector('$1').getBoundingClientRect().top + window.scrollY - ${2:-70})); 'ok'"; }

fetch() { # fetch <path-with-query> <outfile>
  curl -sS -m 30 -H "Authorization: Bearer $DEMO_TOKEN" -H "X-Termini-Version: 1" \
    "$API/$1" -o "$2" -w "%{http_code}" > "$TMP/code.txt"
  log "  GET $API/$1 -> HTTP $(cat "$TMP/code.txt") ($(stat -c%s "$2") bytes)"
}

# First row of whatever the panel is currently filtered to, as the DOM renders it
FIRST_ROW_JS='(() => {
  const r = document.querySelector("#decision-list .decision-row");
  if (!r) return JSON.stringify({none: true});
  const audit = r.querySelector(".audit button");
  const tel = Array.from(r.querySelectorAll(".decision-links button")).find(b => b.title.indexOf("health event") === 0);
  const dec = document.getElementById("decider");
  return JSON.stringify({
    renderedRow: r.textContent.replace(/\s+/g, " ").trim(),
    rowClass: r.className,
    auditId: audit ? audit.title : null,
    eventId: tel ? tel.title.replace("health event ", "") : null,
    decisionId: r.getAttribute("data-decision-id"),
    filter: Array.from(document.querySelectorAll("[data-decision-filter]")).filter(b => b.getAttribute("aria-pressed") === "true").map(b => b.textContent).join(","),
    decisionsCount: document.getElementById("decisions-count").textContent.trim(),
    deciderText: dec ? dec.textContent.replace(/\s+/g, " ").trim() : null
  });
})()'

PAGE_FACTS_JS='(() => JSON.stringify({
  url: location.href,
  title: document.title,
  logRows: document.querySelectorAll(".panel-logs .log-row").length,
  logState: document.querySelector(".panel-logs .panel-head").textContent.replace(/\s+/g, " ").trim(),
  decisionsCount: document.getElementById("decisions-count").textContent.trim(),
  deciderText: (document.getElementById("decider") || {}).textContent ? document.getElementById("decider").textContent.replace(/\s+/g, " ").trim() : null,
  streamState: document.querySelector(".panel-logs .panel-head").textContent.replace(/\s+/g, " ").trim()
}))()'

wait_rows() { # wait until the decisions panel has rows
  for _ in $(seq 1 30); do
    case "$(js 'document.querySelectorAll("#decision-list .decision-row").length')" in
      '{"value": 0}'|*'"value": 0'*) sleep 2 ;;
      *) return 0 ;;
    esac
  done
  log "!! no decision rows appeared"; return 1
}

wait_logs() { # wait until the log panel rendered lines (the defect this card fixes)
  for _ in $(seq 1 30); do
    n="$(jst 'document.querySelectorAll(".panel-logs .log-row").length')"
    if [ "${n:-0}" -gt 5 ]; then log "  log rows rendered: $n"; return 0; fi
    sleep 2
  done
  log "!! log panel still empty"; return 1
}

header() { # header <file> <n> <slug> <url> <purpose>
  {
    printf '# Sidecar for %s (acceptance %s)\n' "$2-$3.png" "$2"
    printf '# Purpose: %s\n' "$5"
    printf '# Captured (UTC): %s\n' "$(now)"
    printf '# Exact URL in the address bar: %s\n' "$4"
    printf '# Window: %sx%s, headful Chromium in Xvfb :99, browser chrome included, no crop/annotation.\n' "$W" "$H"
    printf '# Raw text behind the pixels follows.\n\n'
  } > "$1"
}

########## preflight ##########################################################
log "== $(now) preflight: published page + live feed =="
js "location.href = '$URL_SYNTH'" >/dev/null
sleep 12
for attempt in 1 2 3 4 5; do
  NROWS="$(js 'document.querySelectorAll("#decision-list .decision-row").length')"
  log "  attempt $attempt: decision rows = $NROWS"
  case "$NROWS" in *'"value": 0'*) js 'location.reload()' >/dev/null; sleep 15 ;; *) break ;; esac
done
wait_rows
close_dialogs

########## 01: a live MODEL decision (acceptance a) ###########################
log "== $(now) 01 model decision =="
js 'document.querySelector("[data-decision-filter=model]").click()' >/dev/null
sleep 5
scroll_to '.panel-decisions'
sleep 2
shot 01-model-decision.png
FACTS1="$(jst "$FIRST_ROW_JS")"
log "  $FACTS1"
fetch "operator/decisions?limit=200" "$TMP/decisions.json"
fetch "operator/audits?limit=200" "$TMP/audits.json"
fetch "operator/events?limit=200" "$TMP/events.json"
AUDIT_ID="$(jst 'document.querySelector("#decision-list .decision-row .audit button").title')"
EVENT_ID="$(jst '(() => {const b = Array.from(document.querySelectorAll("#decision-list .decision-row .decision-links button")).find(x => x.title.indexOf("health event") === 0); return b ? b.title.replace("health event ", "") : ""})()')"
PAGE1="$(jst "$PAGE_FACTS_JS")"
{
  header "$OUT/01-model-decision.txt" 01 model-decision "$URL_SYNTH" \
    "a live decision whose decision_source=model: breaching metric + threshold, the model rationale, the chosen action + target, the execution result and the audit id"
  printf '## As rendered on the published page (read back out of the live DOM)\n%s\n\n' "$FACTS1"
  printf '## Page facts\n%s\n\n' "$PAGE1"
  printf '## Raw API object behind that row (newest decision with decision_source=model)\n'
  printf 'GET %s/operator/decisions?limit=200   (Authorization: Bearer $DEMO_TOKEN, X-Termini-Version: 1)\n\n' "$API"
  python3 "$TOOLS/extract.py" decision "$TMP/decisions.json" model
  printf '\n## The audit row for that decision (the id shown in the row)\n'
  printf 'GET %s/operator/audits?limit=200\n\n' "$API"
  python3 "$TOOLS/extract.py" audit "$TMP/audits.json" "$AUDIT_ID"
  printf '\n## The telemetry sample that fired it (the row links to this health event)\n'
  printf 'GET %s/operator/events?limit=200\n\n' "$API"
  python3 "$TOOLS/extract.py" event "$TMP/events.json" "$EVENT_ID"
} >> "$OUT/01-model-decision.txt"

########## 02: a live FALLBACK decision (acceptance b) ########################
log "== $(now) 02 fallback decision =="
js 'document.querySelector("[data-decision-filter=fallback]").click()' >/dev/null
sleep 5
scroll_to '.panel-decisions'
sleep 2
shot 02-fallback-decision.png
FACTS2="$(jst "$FIRST_ROW_JS")"
log "  $FACTS2"
{
  header "$OUT/02-fallback-decision.txt" 02 fallback-decision "$URL_SYNTH" \
    "the same panel showing a decision the MODEL did not make: decision_source=fallback, no confidence, with fallback_reason instead of a rationale - rendered in a visibly different style from 01"
  printf '## As rendered on the published page (read back out of the live DOM)\n%s\n\n' "$FACTS2"
  printf '## Raw API object behind that row (newest decision with decision_source=fallback)\n'
  printf 'GET %s/operator/decisions?limit=200\n\n' "$API"
  python3 "$TOOLS/extract.py" decision "$TMP/decisions.json" fallback
  printf '\n## For contrast, the same call for a model decision (confidence + rationale, no fallback_reason)\n\n'
  python3 "$TOOLS/extract.py" decision "$TMP/decisions.json" model
} >> "$OUT/02-fallback-decision.txt"

########## 03: the log panel renders events on the published URL (acceptance c)
log "== $(now) 03 log panel (plain published URL) =="
js "location.href = '$URL_PLAIN'" >/dev/null
sleep 12
wait_logs
scroll_to '.panel-logs .panel-head'
sleep 2
shot 03-log-panel-events.png
PAGE3="$(jst "$PAGE_FACTS_JS")"
log "  $PAGE3"
curl -sS -N -m 8 "$API/projects/$PROJECT/logs?token=$DEMO_TOKEN&since=00000000000000000000000000" \
  -o "$TMP/sse.txt" || true
{
  header "$OUT/03-log-panel-events.txt" 03 log-panel-events "$URL_PLAIN" \
    "the log panel rendering events (not the reported 0-lines empty state) on the plain published URL - the same URL the README/SUBMISSION hand to a viewer"
  printf '## As rendered on the published page (read back out of the live DOM)\n%s\n\n' "$PAGE3"
  printf '## The stream the panel consumes (first connect sends the contract-free cursor)\n'
  printf 'curl -N "%s/projects/%s/logs?token=$DEMO_TOKEN&since=00000000000000000000000000"   (last lines)\n\n' "$API" "$PROJECT"
  python3 "$TOOLS/extract.py" sse "$TMP/sse.txt" 25
} >> "$OUT/03-log-panel-events.txt"

########## 04: the decider label (acceptance d) ##############################
log "== $(now) 04 decider label =="
scroll_to '#decider'
sleep 2
shot 04-decider-label.png
DECIDER="$(jst 'document.getElementById("decider").textContent.replace(/\s+/g, " ").trim()')"
EGRESS="$(grep -m 1 -o 'externalModelCall.*' "$TMP/sse.txt" || echo 'NOT FOUND IN CAPTURE')"
log "  decider: $DECIDER"
{
  header "$OUT/04-decider-label.txt" 04 decider-label "$URL_PLAIN" \
    "the honest WHO-DECIDED label, derived from the egress record on the operator log stream, not hardcoded: forwarded managed model + backend/model/destination"
  printf '## As rendered on the published page (read back out of the live DOM)\n%s\n\n' "$DECIDER"
  printf '## The egress record it is derived from (first externalModelCall line in the operator log stream)\n\n%s\n\n' "$EGRESS"
  printf '## The line in context (last lines of the same stream capture)\n\n'
  python3 "$TOOLS/extract.py" sse "$TMP/sse.txt" 12
} >> "$OUT/04-decider-label.txt"

########## 05: the audit detail dialog (supports acceptance a) ################
log "== $(now) 05 audit detail dialog =="
js "location.href = '$URL_SYNTH'" >/dev/null
sleep 14
wait_rows
js 'document.querySelector("[data-decision-filter=model]").click()' >/dev/null
sleep 5
AUDIT_ID="$(jst 'document.querySelector("#decision-list .decision-row .audit button").title')"
js 'document.querySelector("#decision-list .decision-row .audit button").click()' >/dev/null
sleep 3
shot 05-audit-detail-dialog.png
DLG="$(jst '(() => {const d = document.querySelector("dialog[open]"); return d ? d.textContent.replace(/\s+/g, " ").trim() : "NO DIALOG"})()')"
log "  dialog: ${DLG:0:300}"
{
  header "$OUT/05-audit-detail-dialog.txt" 05 audit-detail-dialog "$URL_SYNTH" \
    "the audit record behind the row in 01, in full (audit id, result, endpoint, LXD call, idempotency key, detail) - what the client checks after an action"
  printf '## The dialog as rendered (read back out of the live DOM)\n%s\n\n' "$DLG"
  printf '## Raw audit object as the API returned it\n'
  printf 'GET %s/operator/audits?limit=200\n\n' "$API"
  fetch "operator/audits?limit=200" "$TMP/audits.json" >/dev/null
  python3 "$TOOLS/extract.py" audit "$TMP/audits.json" "$AUDIT_ID"
  printf '\n## Raw decision object for the decision this audit executed\n'
  python3 "$TOOLS/extract.py" decision "$TMP/decisions.json" any
} >> "$OUT/05-audit-detail-dialog.txt"
close_dialogs

log "== done: $(now) =="
ls -la "$OUT"
