#!/usr/bin/env bash
# Re-runnable capture for card t_4386b903 — the two frames that card owes, taken
# against the PUBLISHED deployment (Firebase Hosting) and the standing
# controller, in an approved live window.
#
#   03-log-panel-events-red.png    the log panel + its stream chip, as it really is
#   04-decider-label-red.png       the decider band, as it really is
#   06-chart-fault-recovery.png    the memory chart with an OPERATOR-INJECTED fault:
#                                  ram rises above the rule's threshold, the decision
#                                  markers land on the rise, and the series recovers
#                                  after the fault is cleared
# each with a .txt sidecar carrying the raw API/stream output behind the pixels,
# plus client/verification.txt with the capture log.
#
# 03/04 are named `-red` ON PURPOSE: the acceptance asks for frames whose stream
# chip reads `live · resumed from the oldest retained line`, and on this
# deployment they cannot — `GET /v1/projects/{pid}/logs` authenticates from the
# `?token` QUERY ONLY (controller/app/server.py `_handle_sse_logs`), while the
# shipped dashboard sends the credential in the Authorization header and
# additionally sends a `Cache-Control` request header the controller's CORS
# allow-list does not cover. Both are proven by the probes this script runs and
# pastes into those sidecars. The frames are still captured, labelled, rather
# than left as a silent hole.
#
# The fault frame does NOT need anyone else's cooperation: the fault is injected
# through the documented operator endpoint (controller-api.md §4.7) on a
# DEMO (synthetic) node, held, then CLEARED — so the same 15m window shows the
# rise, the decision and the recovery. Nothing is simulated on the client: the
# page is the published bundle, the charts are fed by the controller, and the
# numbers in the sidecar are the exact payload the page received.
#
# Prereqs: Xvfb, xdotool, xdpyinfo, ffmpeg, the bundled Chromium, jq, curl.
# Usage:
#   bash tools/start-browser.sh "https://termini-510609.web.app/?apiBase=...&projectId=..."
#   bash tools/capture-client-live.sh            # all steps
#   STEPS="06" bash tools/capture-client-live.sh # just the chart frame
set -uo pipefail

HOSTING="${HOSTING:-https://termini-510609.web.app}"
API="${API:-https://34-93-190-64.sslip.io/v1}"
PROJECT="${PROJECT:-01J0DEM0WARM000000000000AB}"
# The published demo project token. It is pasted into the dashboard's own
# Settings dialog and kept in that tab's sessionStorage: never in a URL.
DEMO_TOKEN="${DEMO_TOKEN:-demo-project-token}"

# The node the fault is injected on: `demo-node-a`, a SEEDER node (synthetic) of
# the demo project — labelled "(demo)" by the dashboard — so the injected series
# never touches real-hardware telemetry another card may be measuring.
FAULT_NODE="${FAULT_NODE:-01M4NZK46WXG5ZEFAGAAV5JRXM}"
FAULT_SCENARIO="${FAULT_SCENARIO:-oom}"
case "$FAULT_SCENARIO" in
  oom) FAULT_RULE="oom-guard-01" ;;
  disk-degrade) FAULT_RULE="disk-degrade-01" ;;
  disk-fail) FAULT_RULE="disk-fail-01" ;;
  latency) FAULT_RULE="latency-01" ;;
  *) FAULT_RULE="" ;;
esac
PREWAIT_S="${PREWAIT_S:-0}"  # optional: wait before injecting (previous episode out of the window)
HOLD_S="${HOLD_S:-240}"     # how long the fault is held before it is cleared
SETTLE_S="${SETTLE_S:-25}"  # after the clear, so the recovery is in the window
STEPS="${STEPS:-03 04 06}"
export DISPLAY="${DISPLAY_NUM:-:99}"

EVID="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$EVID/client"
TOOLS="$EVID/tools"
TMP="${TMPDIR:-/tmp}/termini-capture-live"
PORT="${PORT:-9222}"
FFMPEG="${FFMPEG:-/home/ubuntu/.hermes/tools/ffmpeg-9.0.1-linux-x64/bin/ffmpeg}"
W="${W:-1280}"
H="${H:-1700}"   # tall enough that one decision row + the memory chart share a frame
mkdir -p "$OUT" "$TMP"
: > "$OUT/verification.txt"

log()  { printf '%s\n' "$*" | tee -a "$OUT/verification.txt"; }
now()  { date -u +%Y-%m-%dT%H:%M:%SZ; }
js()   { cdp_retry "$1" json; }
jst()  { cdp_retry "$1" text; }
cdp_retry() {
  local expr="$1" mode="$2" out
  for _ in 1 2 3 4 5 6; do
    if [ "$mode" = "text" ]; then
      out="$(python3 "$TOOLS/cdp_eval.py" "$PORT" --text "$expr" 2>/dev/null)" || true
    else
      out="$(python3 "$TOOLS/cdp_eval.py" "$PORT" "$expr" 2>/dev/null)" || true
    fi
    if [ -n "$out" ]; then printf '%s\n' "$out"; return 0; fi
    sleep 3
  done
  printf '%s\n' '{"error":"cdp evaluate failed after 6 attempts"}'
  return 1
}
close_dialogs() { js '(() => {const d = document.querySelector("dialog[open]"); if (d) d.close(); return "dialogs closed"})()' >/dev/null; }
shot() {
  "$FFMPEG" -hide_banner -loglevel error -y -f x11grab -video_size "${W}x${H}" \
    -i :99 -frames:v 1 "$OUT/$1"
  log "  shot $1 -> $(stat -c%s "$OUT/$1") bytes"
}
# scroll so the given selector's top sits `off` px below the viewport top
# (the dashboard header bar is sticky and overlays the first ~57 px).
scroll_to() { js "window.scrollTo(0, Math.max(0, document.querySelector('$1').getBoundingClientRect().top + window.scrollY - ${2:-70})); 'ok'"; }
# put the browser window back to the capture size ${W}x${H} and drop any device
# emulation an earlier step left behind (a later step resizes the window for a
# one-column layout; the grab follows W/H, so both must travel together).
reset_window() {
  python3 "$TOOLS/cdp_do.py" "$PORT" clear-viewport >/dev/null 2>&1 || true
  xdotool search --onlyvisible --name Termini windowsize "$W" "$H" 2>/dev/null || true
  xdotool search --onlyvisible --name Termini windowmove 0 0 2>/dev/null || true
  sleep 2
}

# GET an API path with the project token in the Authorization header.
fetch_api() { # fetch_api <path> <outfile>
  curl -sS -m 40 -H "Authorization: Bearer ${DEMO_TOKEN}" -H "X-Termini-Version: 1" \
    "$API/$1" -o "$2" -w "%{http_code}" > "$TMP/code.txt" 2>/dev/null
  log "  GET $API/$1 -> HTTP $(cat "$TMP/code.txt") ($(stat -c%s "$2" 2>/dev/null || echo 0) bytes)"
}

header() { # header <file> <n> <slug> <url> <purpose>
  {
    printf '# Sidecar for %s (step %s)\n' "$2-$3.png" "$2"
    printf '# Purpose: %s\n' "$5"
    printf '# Captured (UTC): %s\n' "$(now)"
    printf '# Exact URL in the address bar: %s\n' "$4"
    printf '# Window: %sx%s, headful Chromium in Xvfb :99, browser chrome included, no crop/annotation.\n' "$W" "$H"
    printf '# Credential: the published demo project token, pasted into the dashboard Settings dialog and kept in this tab only (sessionStorage) — never in the URL.\n'
    printf '# Raw text behind the pixels follows.\n\n'
  } > "$1"
}

PAGE_FACTS_JS='(() => JSON.stringify({
  url: location.href,
  logRows: document.querySelectorAll(".panel-logs .log-row").length,
  logOpen: (document.getElementById("log-panel") || {}).open,
  streamChip: (document.getElementById("log-state") || {}).textContent ? document.getElementById("log-state").textContent.replace(/\s+/g, " ").trim() : null,
  decisionsCount: (document.getElementById("decisions-count") || {}).textContent ? document.getElementById("decisions-count").textContent.trim() : null,
  decider: (document.getElementById("decider") || {}).textContent ? document.getElementById("decider").textContent.replace(/\s+/g, " ").trim() : null,
  banner: (document.querySelector("#banner, .banner") || {}).textContent ? document.querySelector("#banner, .banner").textContent.replace(/\s+/g, " ").trim() : null,
  headerPills: Array.from(document.querySelectorAll(".appbar .pill, header .pill")).map(p => p.textContent.replace(/\s+/g, " ").trim())
}))()'

########## preflight ##########################################################
log "== $(now) preflight: published page, sign-in through the Settings dialog"
reset_window
js "location.href = '${HOSTING}/?apiBase=${API}&projectId=${PROJECT}'; 'navigated'" >/dev/null
sleep 12
# The credential is never a query parameter (t_5649d117): a viewer pastes it into
# the Settings dialog, which keeps it in sessionStorage for this tab.
SIGNIN="$(jst '(() => {const need = document.querySelectorAll("#decision-list .decision-row").length === 0 || /UNAUTHENTICATED/.test(document.body.textContent); if (!need) return "already signed in"; document.getElementById("btn-settings").click(); return "dialog open";})()')"
log "  sign-in: $SIGNIN"
if [ "$SIGNIN" = "dialog open" ]; then
  # the token is handed to the page by name, never typed into a shell history
  js "document.getElementById('set-token').value = '${DEMO_TOKEN}'; document.getElementById('set-save').click(); 'saved'" >/dev/null
  sleep 10
fi
for _ in $(seq 1 6); do
  N="$(js 'document.querySelectorAll("#decision-list .decision-row").length')"
  log "  decision rows: $N"
  case "$N" in *'"value": 0'*|*'"value":0'*|'0') sleep 5 ;; *) break ;; esac
done
log "  preflight page: $(jst "$PAGE_FACTS_JS")"

########## 03: the log panel and its stream chip (acceptance b, AS IT IS) #####
if [[ " $STEPS " == *" 03 "* ]]; then
  log "== $(now) 03 log panel (plain published URL)"
  # the panel is a <details>: a viewer expands it to see the raw operator stream
  js '(() => {const d = document.getElementById("log-panel"); if (!d.open) d.querySelector("summary").click(); return "open=" + d.open})()' >/dev/null
  sleep 8
  scroll_to '.panel-logs .panel-head'
  sleep 2
  shot 03-log-panel-events-red.png
  PAGE3="$(jst "$PAGE_FACTS_JS")"
  log "  $PAGE3"
  # Proof of WHY the chip cannot read `live` on this deployment: two probes of the
  # same endpoint, one per authentication style.
  curl -sS -m 12 -D "$TMP/logs_header.hdr" -H "Authorization: Bearer ${DEMO_TOKEN}" -H "X-Termini-Version: 1" \
    "$API/projects/${PROJECT}/logs?since=00000000000000000000000000" -o "$TMP/logs_header.body" 2>/dev/null || true
  ( timeout 8 curl -sS -N -D "$TMP/logs_query.hdr" "$API/projects/${PROJECT}/logs?token=${DEMO_TOKEN}&since=00000000000000000000000000" -o "$TMP/logs_query.body" ) 2>/dev/null || true
  # and the same fetch the panel itself makes, with and without the extra header
  STREAM_PROBE="$(jst 'Promise.all([
    fetch("'"$API"'/projects/'"$PROJECT"'/logs?since=00000000000000000000000000", {headers:{Accept:"text/event-stream","Cache-Control":"no-cache","X-Termini-Version":"1",Authorization:"Bearer '"$DEMO_TOKEN"'"}, cache:"no-store"}).then(r=>"with Cache-Control: HTTP "+r.status).catch(e=>"with Cache-Control: THREW "+e.name+": "+e.message),
    fetch("'"$API"'/projects/'"$PROJECT"'/logs?since=00000000000000000000000000", {headers:{Accept:"text/event-stream","X-Termini-Version":"1",Authorization:"Bearer '"$DEMO_TOKEN"'"}, cache:"no-store"}).then(r=>"Authorization header only: HTTP "+r.status).catch(e=>"Authorization header only: THREW "+e.name+": "+e.message)
  ]).then(a=>a.join(" | "))')"
  {
    header "$OUT/03-log-panel-events-red.txt" 03 log-panel-events-red \
      "${HOSTING}/?apiBase=${API}&projectId=${PROJECT}" \
      "the log panel as it really renders on the published URL, with its stream chip. THIS IS NOT THE ACCEPTANCE FRAME: the card asks for a chip reading 'live · resumed from the oldest retained line' and this deployment cannot produce one — the proofs are below, and the frame is kept labelled rather than left as a silent hole."
    printf '## As rendered on the published page (read back out of the live DOM)\n%s\n\n' "$PAGE3"
    printf '## Why the chip reads what it reads — the endpoint, probed three ways\n'
    printf '### 1. The page`s own fetch, exactly as js/stream.js makes it\n%s\n\n' "$STREAM_PROBE"
    printf '### 2. curl with the Authorization header (what the dashboard sends)\n'
    head -1 "$TMP/logs_header.hdr"; printf '\n'; cat "$TMP/logs_header.body"; printf '\n\n'
    printf '### 3. curl with ?token= (the only style the controller accepts here)\n'
    head -1 "$TMP/logs_query.hdr"; printf '\n'
    python3 "$TOOLS/extract.py" sse "$TMP/logs_query.body" 8 2>/dev/null || printf '(no stream body)\n'
    printf '\n## The controller code that decides it (origin/main)\n'
    printf 'controller/app/server.py:_handle_sse_logs -> self._require_project_token(q.get("token"))\n'
    printf '  — the Authorization header is not consulted on this route, while every other\n'
    printf '  route reads it. dashboard/js/stream.js sends the credential ONLY in the header\n'
    printf '  (t_5649d117 forbids it in a URL) and also sends a `Cache-Control` request\n'
    printf '  header that the controller`s CORS allow-list does not include, so the\n'
    printf '  preflight fails and fetch() rejects before a status is ever seen.\n'
  } >> "$OUT/03-log-panel-events-red.txt"
fi

########## 04: the decider band (acceptance d, AS IT IS) #####################
if [[ " $STEPS " == *" 04 "* ]]; then
  log "== $(now) 04 decider label (plain published URL)"
  scroll_to '#decider' 90
  sleep 2
  shot 04-decider-label-red.png
  PAGE4="$(jst "$PAGE_FACTS_JS")"
  DECIDER="$(jst 'document.getElementById("decider").textContent.replace(/\s+/g, " ").trim()')"
  log "  decider: $DECIDER"
  {
    header "$OUT/04-decider-label-red.txt" 04 decider-label-red \
      "${HOSTING}/?apiBase=${API}&projectId=${PROJECT}" \
      "the decider band as it really renders. NOT THE ACCEPTANCE FRAME: the label is derived from the operator log stream, and that stream cannot authenticate from the dashboard on this deployment (proof in the 03 sidecar) — so the band shows its honest unknown state."
    printf '## As rendered on the published page (read back out of the live DOM)\n%s\n\n' "$DECIDER"
    printf '## Page facts at the capture instant\n%s\n\n' "$PAGE4"
    printf '## What would populate it\n'
    printf 'The band is parsed from the first `externalModelCall` line on the operator log\n'
    printf 'stream (GET %s/projects/%s/logs). With the stream unreadable from the browser\n' \
      "$API" "$PROJECT"
    printf 'the label cannot be derived, and the panel says so instead of inventing one.\n'
  } >> "$OUT/04-decider-label-red.txt"
fi

########## 06: the chart frame for an injected fault ##########################
if [[ " $STEPS " == *" 06 "* ]]; then
  log "== $(now) 06 chart fault frame (synthetic URL, 15m window)"
  URL_SYNTH="${HOSTING}/?apiBase=${API}&projectId=${PROJECT}&synthetic=1&decisionLimit=200"
  js "location.href = '${URL_SYNTH}'; 'navigated'" >/dev/null
  sleep 14
  # the URL carries no credential, so the tab is still signed in; assert rows
  for _ in $(seq 1 8); do
    N="$(js 'document.querySelectorAll("#decision-list .decision-row").length')"
    log "  decision rows: $N"
    case "$N" in *'"value": 0'*|*'"value":0'*|'0') sleep 5 ;; *) break ;; esac
  done
  close_dialogs

  # ---- clear anything already open on this (node, rule) — a rerun must not 409
  fetch_api "projects/${PROJECT}/demo/faults?state=open" "$TMP/faults_open.json"
  OPEN_ID="$(jq -r --arg n "$FAULT_NODE" '.faults[]? | select(.node_id == $n) | .injection_id' "$TMP/faults_open.json" 2>/dev/null | head -1)"
  if [ -n "${OPEN_ID:-}" ]; then
    log "  clearing a fault left open by an earlier run: $OPEN_ID"
    curl -sS -m 20 -X DELETE -H "Authorization: Bearer ${DEMO_TOKEN}" \
      "$API/projects/${PROJECT}/demo/faults/${OPEN_ID}?reason=rerun_cleanup" -o "$TMP/fault_cleanup.json" -w '%{http_code}\n' 2>/dev/null | tail -1 | sed 's/^/  DELETE -> HTTP /' | tee -a "$OUT/verification.txt"
    sleep 6
  fi

  # ---- inject the fault through the documented operator endpoint (§4.7)
  if [ "${PREWAIT_S:-0}" -gt 0 ]; then
    log "  waiting ${PREWAIT_S}s before injecting, so the previous episode has left the 15m window"
    sleep "$PREWAIT_S"
  fi
  log "  injecting $FAULT_SCENARIO on $FAULT_NODE (hold ${HOLD_S}s, then clear)"
  TTL=$(( HOLD_S + 180 ))
  START_CODE="$(curl -sS -m 25 -X POST "$API/projects/${PROJECT}/demo/faults" \
      -H "Authorization: Bearer ${DEMO_TOKEN}" -H "X-Termini-Version: 1" -H "Content-Type: application/json" \
      -d "{\"node_id\":\"${FAULT_NODE}\",\"scenario\":\"${FAULT_SCENARIO}\",\"duration_s\":${TTL},\"target\":\"synthetic\"}" \
      -o "$TMP/fault_start.json" -w '%{http_code}' 2>/dev/null)"
  INJECTION_ID="$(jq -r '.injection_id // empty' "$TMP/fault_start.json" 2>/dev/null)"
  INJECTED_AT="$(jq -r '.started_at // empty' "$TMP/fault_start.json" 2>/dev/null)"
  log "  POST demo/faults -> HTTP ${START_CODE} injection=${INJECTION_ID:-none} started=${INJECTED_AT:-?}"
  if [ -z "${INJECTION_ID:-}" ]; then
    log "!! NOT CAPTURED: the controller refused the injection — see $TMP/fault_start.json"
    jq . "$TMP/fault_start.json" 2>/dev/null | tee -a "$OUT/verification.txt"
  else
    # ---- wait for the rule to fire and a decision to be recorded for this node
    DEC_ID=""
    for _ in $(seq 1 24); do
      sleep 5
      fetch_api "operator/decisions?limit=200" "$TMP/decisions.json" >/dev/null
      DEC_ID="$(jq -r --arg n "$FAULT_NODE" --arg t "$INJECTED_AT" '[.decisions[]? | select(.node_id == $n and ((.decided_at // "") >= $t))] | .[0].id // empty' "$TMP/decisions.json" 2>/dev/null)"
      if [ -n "${DEC_ID:-}" ]; then log "  decision on the injected node: $DEC_ID"; break; fi
    done

    # ---- hold, then clear (the clear starts the controller's recovery burst)
    REMAIN=$(( HOLD_S - 30 )); [ "$REMAIN" -gt 0 ] && { log "  holding ${REMAIN}s"; sleep "$REMAIN"; }
    log "  clearing the fault $(now)"
    CLEAR_CODE="$(curl -sS -m 25 -X DELETE -H "Authorization: Bearer ${DEMO_TOKEN}" -H "X-Termini-Version: 1" \
        "$API/projects/${PROJECT}/demo/faults/${INJECTION_ID}?reason=capture_complete" \
        -o "$TMP/fault_clear.json" -w '%{http_code}' 2>/dev/null)"
    log "  DELETE demo/faults/${INJECTION_ID} -> HTTP ${CLEAR_CODE} state=$(jq -r '.state // "?"' "$TMP/fault_clear.json" 2>/dev/null)"
    log "  settling ${SETTLE_S}s so the recovery is inside the same 15m window"
    sleep "$SETTLE_S"

    # ---- verify the window the chart is about to draw really holds the fault
    START="$(date -u -d '-15 minutes' +%Y-%m-%dT%H:%M:%SZ)"
    END="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    fetch_api "projects/${PROJECT}/nodes/${FAULT_NODE}/metrics?start=${START}&end=${END}&resolution=5s" "$TMP/metrics_check.json"
    python3 "$TOOLS/series.py" "$TMP/metrics_check.json" --metric ram_used_bytes --threshold-pct 95 > "$TMP/series_check.txt" 2>&1
    SERIES_RC=$?
    log "  series check: exit ${SERIES_RC} — $(grep '^RESULT' "$TMP/series_check.txt" | head -1)"

    # ---- re-pick the decision to plot NOW: the panel is newest-first and the
    # fault keeps producing a decision every few seconds while it is held, so the
    # id chosen at fire time has already sunk down the list. Plotting the newest
    # decision for the node keeps its row at the top, where it can share a frame
    # with the chart.
    fetch_api "operator/decisions?limit=200" "$TMP/decisions.json"
    NEWEST="$(jq -r --arg n "$FAULT_NODE" --arg t "$INJECTED_AT" --arg r "$FAULT_RULE" '[.decisions[]? | select(.node_id == $n and .rule_id == $r and ((.decided_at // "") >= $t))] | .[0].id // empty' "$TMP/decisions.json" 2>/dev/null)"
    if [ -n "${NEWEST:-}" ]; then DEC_ID="$NEWEST"; fi
    log "  plotting decision ${DEC_ID} (newest ${FAULT_RULE} decision for the injected node)"

    # ---- one-column window BEFORE plotting, so the resize cannot drop the
    #      annotations the click just attached (the 1280 px layout puts the
    #      charts in a 466 px rail next to a 2 875 px collapsed log panel, which
    #      renders this card at 444x150 in a black void). At 1000 px the dashboard
    #      is one column and the same card is ~911 px wide. The browser WINDOW is
    #      resized (xdotool) rather than the page being emulated, and the grab is
    #      the same size as the window: chrome included, nothing cropped.
    if [ "${NARROW:-1}" = "1" ]; then
      W="${NARROW_W:-1000}"
      python3 "$TOOLS/cdp_do.py" "$PORT" clear-viewport >/dev/null 2>&1 || true
      xdotool search --onlyvisible --name Termini windowsize "$W" "$H" 2>/dev/null || true
      xdotool search --onlyvisible --name Termini windowmove 0 0 2>/dev/null || true
      sleep 4
    fi
    # the 15m window is what the card asks for; set it before plotting
    log "  $(js '(() => {const b = [...document.querySelectorAll("#window-buttons button")].find(x => /^15m/.test((x.textContent||"").trim()) && !x.classList.contains("on")); if (b) b.click(); return "window: " + [...document.querySelectorAll("#window-buttons button")].map(x => x.textContent.trim() + (x.classList.contains("on") ? "*" : "")).join(",");})()')"
    sleep 3

    # ---- install a fetch recorder so the sidecar can carry the EXACT payload the
    #      chart drew (not a re-fetch a minute later)
    js '(() => { if (window.__capOn) return "already on"; window.__capOn = true; window.__cap = [];
      const of = window.fetch;
      window.fetch = function (...a) { return of.apply(this, a).then(async (r) => {
        try { const u = typeof a[0] === "string" ? a[0] : (a[0] && a[0].url) || "";
          if (String(u).indexOf("/metrics") !== -1) { const c = r.clone(); window.__cap.push({url:String(u), status:r.status, body: await c.text()}); }
        } catch (e) {} return r; }); };
      return "recorder on"; })()' >/dev/null

    # ---- click `show on chart` on the decision row for the injected episode
    CLICKED="$(js "(() => {const r = document.querySelector('[data-decision-id=\"${DEC_ID}\"]'); if (!r) return 'row not in the DOM'; const b = r.querySelector('button[title^=\"plot \"]'); if (!b) return 'no plot link'; b.click(); return 'clicked: ' + b.title;})()")"
    log "  $CLICKED"
    sleep 4
    CAPN=0
    for _ in $(seq 1 12); do
      CAPN="$(jst 'window.__cap ? window.__cap.length : 0')"
      case "$CAPN" in ''|'0') sleep 3 ;; *) break ;; esac
    done
    log "  metrics responses recorded by the page: ${CAPN}"
    sleep 3   # let the canvas redraw with markers + threshold

    # ---- frame: the memory chart card, legible, in one frame.
    FRAME_JS="(() => {
      const card = document.querySelector('[data-chart=\"mem\"]');
      if (!card) return JSON.stringify({error: 'no memory chart card'});
      const chips = document.getElementById('window-buttons');
      const vh = innerHeight;
      const anchorAbs = (chips ? chips.getBoundingClientRect().top : card.getBoundingClientRect().top) + scrollY;
      window.scrollTo(0, Math.max(0, anchorAbs - 110));
      const ct = chips ? chips.getBoundingClientRect().top : 0;
      if (ct < 60) window.scrollTo(0, Math.max(0, scrollY - (60 - ct)));
      const q = card.getBoundingClientRect();
      const cq = card.querySelector('canvas') ? card.querySelector('canvas').getBoundingClientRect() : null;
      const chipList = chips ? Array.from(chips.querySelectorAll('button')).map(b => b.textContent.trim() + (b.classList.contains('on') ? '*' : '')) : [];
      return JSON.stringify({vw: innerWidth, vh, scrollY: Math.round(scrollY),
        chipTop: chips ? Math.round(ct) : null,
        cardTop: Math.round(q.top), cardBottom: Math.round(q.bottom),
        canvas: cq ? {w: Math.round(cq.width), h: Math.round(cq.height)} : null,
        cardClass: card.className, windowChips: chipList,
        cardFullyVisible: q.top >= 58 && q.bottom <= vh - 4,
        faultCard: card.className.split(' ').indexOf('fault') !== -1,
        thresholdInCaption: card.textContent.indexOf('threshold:') !== -1,
        caption: card.textContent.trim().slice(0, 600)});
    })()"
    FRAME=""
    for attempt in 1 2 3; do
      FRAME="$(js "$FRAME_JS")"
      log "  frame (attempt $attempt): $FRAME"
      case "$FRAME" in *'cardFullyVisible\":true'*) break ;; esac
      sleep 3
    done
    sleep 2
    TARGET=06-chart-fault-recovery.png
    [ "$SERIES_RC" -ne 0 ] && TARGET=06-chart-fault-recovery-FAILED.png
    # NOTE on the patterns: `js` returns the CDP envelope with the JSON value as a
    # STRING, so the quotes inside it are backslash-escaped on the wire — the match
    # has to be for `\"`. (The first attempt at this card matched `": true` with a
    # space and silently captured every verified frame as UNVERIFIED.)
    case "$FRAME" in
      *'cardFullyVisible\":true'*'faultCard\":true'*) shot "$TARGET" ;;
      *) log "!! the memory chart card did not settle inside the viewport with its fault styling — capturing anyway as 06-chart-fault-recovery-UNVERIFIED.png"
         TARGET=06-chart-fault-recovery-UNVERIFIED.png; shot "$TARGET" ;;
    esac

    # ---- read backs
    CHART_FACTS="$(jst '(() => {const card = document.querySelector("[data-chart=\"mem\"]"); const cv = card.querySelector("canvas"); return JSON.stringify({
      chart: "mem", cardClass: card.className, canvasCss: {w: cv.clientWidth, h: cv.clientHeight},
      windowChips: Array.from(document.querySelectorAll("#window-buttons button")).map(b => b.textContent + (b.classList.contains("on") ? "*" : "")),
      selectedNode: (document.getElementById("chart-node") || {}).value,
      chartText: card.textContent.replace(/\s+/g, " ").trim()
    });})()')"
    ROW_FACTS="$(jst "(() => {const r = document.querySelector('[data-decision-id=\"${DEC_ID}\"]'); return r ? JSON.stringify({decisionId: r.dataset.decisionId, source: r.dataset.source, text: r.textContent.replace(/\s+/g, ' ').trim()}) : 'row gone';})()")"
    log "  chart: $CHART_FACTS"
    log "  row:   $ROW_FACTS"

    # the exact bytes the page received for that chart
    if [ "${CAPN:-0}" != "0" ]; then
      jst 'window.__cap[window.__cap.length - 1].body' > "$TMP/metrics_drawn.json"
      jst 'window.__cap[window.__cap.length - 1].url'  > "$TMP/metrics_drawn.url"
      log "  captured the drawn payload: $(stat -c%s "$TMP/metrics_drawn.json") bytes from $(cat "$TMP/metrics_drawn.url")"
    fi

    # ---- the rule's threshold, verbatim, from the health event behind the row
    fetch_api "operator/events?limit=200" "$TMP/events.json"
    fetch_api "operator/audits?limit=200" "$TMP/audits.json"
    EVENT_ID="$(jq -r --arg id "$DEC_ID" '[.decisions[]? | select(.id == $id) | .event_id] | .[0] // empty' "$TMP/decisions.json" 2>/dev/null)"
    AUDIT_ID="$(jq -r --arg id "$DEC_ID" '[.audits[]? | select(.decision_id == $id) | .id] | .[0] // empty' "$TMP/audits.json" 2>/dev/null)"
    # the operator log is not readable from the browser (see the 03 sidecar) but the
    # endpoint still answers a direct, credentialed fetch: cite the injection lines.
    ( timeout 10 curl -sS -N "$API/projects/${PROJECT}/logs?token=${DEMO_TOKEN}&since=00000000000000000000000000" -o "$TMP/logs_after.body" ) 2>/dev/null || true

    {
      header "$OUT/${TARGET%.png}.txt" 06 chart-fault-recovery "$URL_SYNTH" \
        "the memory chart for the decision's own node with an OPERATOR-INJECTED fault in the 15m window: ram_used_bytes rises above the rule's threshold, oom-guard-01 fires (decision markers on the rise), the fault is cleared and the series recovers — cause (telemetry) and effect (action) in one image"
      printf '## What was done to produce the fault (documented operator endpoint, controller-api.md §4.7)\n'
      printf 'POST %s/projects/%s/demo/faults\n' "$API" "$PROJECT"
      printf '  body: {"node_id":"%s","scenario":"%s","duration_s":%s,"target":"synthetic"}\n' "$FAULT_NODE" "$FAULT_SCENARIO" "$TTL"
      printf '  -> HTTP %s\n' "$START_CODE"
      jq . "$TMP/fault_start.json" 2>/dev/null
      printf '\nDELETE %s/projects/%s/demo/faults/%s?reason=capture_complete\n' "$API" "$PROJECT" "$INJECTION_ID"
      printf '  -> HTTP %s\n' "$CLEAR_CODE"
      jq . "$TMP/fault_clear.json" 2>/dev/null
      printf '\nThe injection writes its samples through the SAME store path a node`s own\n'
      printf 'telemetry uses, so the charts, the frozen rules and the operator loop consume it\n'
      printf 'as if the node reported it; while it is open that node`s own reports are\n'
      printf 'superseded. It is on `demo-node-a`, a SEEDER (synthetic) node of the demo\n'
      printf 'project, which the dashboard labels "(demo)" — no real-hardware telemetry was\n'
      printf 'altered and no compute was started.\n\n'
      printf '## As rendered on the published page (read back out of the live DOM)\n'
      printf '### the decision row that was plotted\n%s\n\n' "$ROW_FACTS"
      printf '### the chart card it selected\n%s\n\n' "$CHART_FACTS"
      printf '### page facts\n%s\n\n' "$(jst "$PAGE_FACTS_JS")"
      printf '## The payload the chart drew\n'
      if [ -s "$TMP/metrics_drawn.url" ]; then
        printf 'GET %s\n' "$(cat "$TMP/metrics_drawn.url")"
        printf '(recorded by a fetch wrapper installed in the page immediately before the\n'
        printf 'click; the response is stored verbatim as client/06-metrics-15m-5s.json — the\n'
        printf 'dashboard requests only the charted metric names, so the response carries\n'
        printf 'those series and not the whole allowlist)\n\n'
      else
        printf '(the recorder saw no /metrics response — the chart may have been served from\n'
        printf 'the previous poll; the numbers below are the independent fetch)\n\n'
      fi
      printf '### The metric as numbers, in the chart`s own 5 s buckets, threshold marked *\n'
      printf '(GET %s/projects/%s/nodes/%s/metrics?start=%s&end=%s&resolution=5s — the same\n' \
        "$API" "$PROJECT" "$FAULT_NODE" "$START" "$END"
      printf 'window and resolution the chart requests, issued at the capture instant)\n\n'
      cat "$TMP/series_check.txt"
      printf '\n## The rule behind the threshold line (raw health event, verbatim)\n'
      printf 'GET %s/operator/events?limit=200  (event %s)\n\n' "$API" "${EVENT_ID:-?}"
      if [ -n "${EVENT_ID:-}" ]; then
        python3 "$TOOLS/extract.py" event "$TMP/events.json" "$EVENT_ID" 2>/dev/null
      else
        printf '(no event id resolved from the decision row)\n'
      fi
      printf '\n## The decision that put the markers on the chart (raw, verbatim)\n'
      printf 'GET %s/operator/decisions?limit=200  (decision %s)\n\n' "$API" "$DEC_ID"
      jq --arg id "$DEC_ID" '[.decisions[]? | select(.id == $id)] | .[0]' "$TMP/decisions.json" 2>/dev/null
      printf '\n## What the fleet answered (raw audit row for that decision)\n'
      printf 'GET %s/operator/audits?limit=200  (audit %s)\n\n' "$API" "${AUDIT_ID:-?}"
      if [ -n "${AUDIT_ID:-}" ]; then
        python3 "$TOOLS/extract.py" audit "$TMP/audits.json" "$AUDIT_ID" 2>/dev/null
      else
        printf '(no audit row for this decision id in the fetched window)\n'
      fi
      printf '\n## The controller`s own operator log for the injection (raw SSE, fetched directly)\n'
      printf 'The panel cannot show these lines (see the 03 sidecar); the endpoint still\n'
      printf 'answers a direct credentialed fetch, so the provenance is cited here:\n\n'
      grep -a -i -m 8 'fault \(injected\|cleared\|clearing\)\|TTL expired\|superseded' "$TMP/logs_after.body" 2>/dev/null | tail -8 | sed 's/^/  /'
    } >> "$OUT/${TARGET%.png}.txt"
    if [ -f "$TMP/metrics_drawn.json" ]; then cp "$TMP/metrics_drawn.json" "$OUT/06-metrics-15m-5s.json"; fi
  fi
fi

log "== done: $(now) =="
ls -la "$OUT"
