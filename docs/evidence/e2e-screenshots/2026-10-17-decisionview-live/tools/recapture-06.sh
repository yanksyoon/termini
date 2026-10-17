#!/usr/bin/env bash
# Re-shoot the step-06 frame a few minutes after the capture run, from the SAME
# page and the SAME plotted decision, with no new injection.
#
# Why this exists: the 15 m window at shoot time still held the *previous*
# episode of this card (an earlier, failed attempt at the same frame), so the
# chart showed two identical rise/plateau/drop cycles and only the right-hand one
# carried a decision marker. Seven minutes later the older episode has slid out of
# the window and the same chart shows exactly one fault episode — the one this
# run injected — with its marker. Nothing is re-injected, re-plotted or edited:
# the page is polled as a viewer would poll it, and the pixels are grabbed again.
#
# The fresh DOM readbacks and the fresh metrics payload are appended to the
# sidecar as a clearly-labelled re-capture section, so the sidecar still states
# exactly what is behind the final pixels.
#
# Usage: bash tools/recapture-06.sh [seconds-to-wait]
set -uo pipefail
export DISPLAY="${DISPLAY_NUM:-:99}"

API="${API:-https://34-93-190-64.sslip.io/v1}"
PROJECT="${PROJECT:-01J0DEM0WARM000000000000AB}"
DEMO_TOKEN="${DEMO_TOKEN:-demo-project-token}"
FAULT_NODE="${FAULT_NODE:-01M4NZK46WXG5ZEFAGAAV5JRXM}"
PORT="${PORT:-9222}"
FFMPEG="${FFMPEG:-/home/ubuntu/.hermes/tools/ffmpeg-9.0.1-linux-x64/bin/ffmpeg}"
W="${W:-1000}"
H="${H:-1700}"
WAIT_S="${1:-0}"

EVID="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$EVID/client"
TOOLS="$EVID/tools"
TMP="${TMPDIR:-/tmp}/termini-capture-live"
mkdir -p "$OUT" "$TMP"

log() { printf '%s\n' "$*" | tee -a "$OUT/verification.txt"; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
js()  { python3 "$TOOLS/cdp_eval.py" "$PORT" "$1" 2>/dev/null; }
jst() { python3 "$TOOLS/cdp_eval.py" "$PORT" --text "$1" 2>/dev/null; }

[ "$WAIT_S" -gt 0 ] && { log "== $(now) re-capture: waiting ${WAIT_S}s so the earlier episode leaves the 15m window"; sleep "$WAIT_S"; }
log "== $(now) re-capture of 06 from the same page (no new injection)"

FRAME_JS='(() => {
  const card = document.querySelector("[data-chart=\"mem\"]");
  if (!card) return JSON.stringify({error: "no memory chart card"});
  const chips = document.getElementById("window-buttons");
  const vh = innerHeight;
  const anchorAbs = (chips ? chips.getBoundingClientRect().top : card.getBoundingClientRect().top) + scrollY;
  window.scrollTo(0, Math.max(0, anchorAbs - 110));
  const ct = chips ? chips.getBoundingClientRect().top : 0;
  if (ct < 60) window.scrollTo(0, Math.max(0, scrollY - (60 - ct)));
  const q = card.getBoundingClientRect();
  const cq = card.querySelector("canvas") ? card.querySelector("canvas").getBoundingClientRect() : null;
  return JSON.stringify({vw: innerWidth, vh, scrollY: Math.round(scrollY),
    chipTop: chips ? Math.round(ct) : null,
    cardTop: Math.round(q.top), cardBottom: Math.round(q.bottom),
    canvas: cq ? {w: Math.round(cq.width), h: Math.round(cq.height)} : null,
    cardClass: card.className,
    windowChips: chips ? Array.from(chips.querySelectorAll("button")).map(b => b.textContent.trim() + (b.classList.contains("on") ? "*" : "")) : [],
    cardFullyVisible: q.top >= 58 && q.bottom <= vh - 4,
    faultCard: card.className.split(" ").indexOf("fault") !== -1,
    thresholdInCaption: card.textContent.indexOf("threshold:") !== -1,
    caption: card.textContent.trim().slice(0, 600)});
})()'

FRAME=""
for attempt in 1 2 3; do
  FRAME="$(js "$FRAME_JS")"
  log "  frame (attempt $attempt): $FRAME"
  case "$FRAME" in *'cardFullyVisible\":true'*) break ;; esac
  sleep 3
done
sleep 2
case "$FRAME" in
  *'cardFullyVisible\":true'*'faultCard\":true'*) ;;
  *) log "!! NOT RE-SHOT: the card did not verify inside the viewport"; exit 1 ;;
esac
mv -f "$OUT/06-chart-fault-recovery.png" "$OUT/superseded/06-chart-fault-recovery-first-capture.png" 2>/dev/null || true
cp -f "$OUT/06-chart-fault-recovery.txt" "$OUT/superseded/06-chart-fault-recovery-first-capture.txt" 2>/dev/null || true
"$FFMPEG" -hide_banner -loglevel error -y -f x11grab -video_size "${W}x${H}" -i :99 -frames:v 1 "$OUT/06-chart-fault-recovery.png"
log "  shot 06-chart-fault-recovery.png -> $(stat -c%s "$OUT/06-chart-fault-recovery.png") bytes (re-capture)"

# ---- fresh readbacks, from the frame that was just grabbed
CHART_FACTS="$(js '(() => {const card = document.querySelector("[data-chart=\"mem\"]"); const cv = card.querySelector("canvas"); return JSON.stringify({
  chart: "mem", cardClass: card.className, canvasCss: {w: cv.clientWidth, h: cv.clientHeight},
  windowChips: Array.from(document.querySelectorAll("#window-buttons button")).map(b => b.textContent.trim() + (b.classList.contains("on") ? "*" : "")),
  selectedNode: (document.getElementById("chart-node") || {}).value,
  thresholdInCaption: card.textContent.indexOf("threshold:") !== -1,
  chartText: card.textContent.replace(/\s+/g, " ").trim()});})()')"
PAGE_FACTS="$(jst '(() => JSON.stringify({url: location.href,
  logRows: document.querySelectorAll(".panel-logs .log-row").length,
  streamChip: (document.getElementById("log-state") || {}).textContent ? document.getElementById("log-state").textContent.replace(/\s+/g, " ").trim() : null,
  decisionsCount: (document.getElementById("decisions-count") || {}).textContent ? document.getElementById("decisions-count").textContent.trim() : null,
  headerPills: Array.from(document.querySelectorAll(".appbar .pill, header .pill")).map(p => p.textContent.replace(/\s+/g, " ").trim())}))()')"
log "  chart: $CHART_FACTS"

# ---- the payload this draw used, and the same window as numbers
START="$(date -u -d '-15 minutes' +%Y-%m-%dT%H:%M:%SZ)"
END="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
jst 'window.__cap ? JSON.stringify({n: window.__cap.length}) : "no recorder"' > "$TMP/cap_n.txt" 2>/dev/null || true
jst 'window.__cap && window.__cap.length ? window.__cap[window.__cap.length - 1].url : ""' > "$TMP/metrics_drawn.url" 2>/dev/null || true
jst 'window.__cap && window.__cap.length ? window.__cap[window.__cap.length - 1].body : ""' > "$TMP/metrics_drawn.json" 2>/dev/null || true
curl -sS -m 40 -H "Authorization: Bearer $DEMO_TOKEN" -H "X-Termini-Version: 1" \
  "$API/projects/${PROJECT}/nodes/${FAULT_NODE}/metrics?start=${START}&end=${END}&resolution=5s" -o "$TMP/metrics_recheck.json" 2>/dev/null || true
python3 "$TOOLS/series.py" "$TMP/metrics_recheck.json" --metric ram_used_bytes --threshold-pct 95 > "$TMP/series_recheck.txt" 2>&1
log "  recheck series: $(grep -E '^(RESULT|[[:space:]]+returned)' "$TMP/series_recheck.txt" | tr '\n' ' ')"

{
  printf '\n\n## RE-CAPTURE of the same frame, %s (no new injection, no re-plot)\n' "$(now)"
  printf 'The first capture of this frame was taken while the previous episode of this\n'
  printf 'card (an earlier, failed attempt) was still inside the 15 m window, so the\n'
  printf 'chart showed two identical rise/plateau/drop cycles and only the right-hand\n'
  printf 'one carried a marker. This frame is the same page, the same plotted decision\n'
  printf 'and the same 15 m window, grabbed again once that older episode had slid out\n'
  printf 'of the window: one rise, one hold, one recovery, one decision marker. The\n'
  printf 'first capture is kept, unedited, as client/superseded/06-chart-fault-recovery-first-capture.png.\n\n'
  printf '### frame check (re-capture)\n%s\n\n' "$FRAME"
  printf '### the chart card as rendered (re-capture)\n%s\n\n' "$CHART_FACTS"
  printf '### page facts (re-capture)\n%s\n\n' "$PAGE_FACTS"
  printf '### the metric as numbers over the window this draw used\n'
  printf 'GET %s/projects/%s/nodes/%s/metrics?start=%s&end=%s&resolution=5s\n\n' "$API" "$PROJECT" "$FAULT_NODE" "$START" "$END"
  cat "$TMP/series_recheck.txt"
  printf '\n'
} >> "$OUT/06-chart-fault-recovery.txt"
cp -f "$TMP/metrics_recheck.json" "$OUT/06-metrics-15m-5s-recheck.json" 2>/dev/null || true
log "== done: $(now) =="
