#!/usr/bin/env bash
# Start (or reuse) the Xvfb display + a HEADFUL Chromium with CDP on :9222.
#
# The evidence convention wants the browser chrome in the image (so the URL is
# visible), which a headless page-grab cannot produce: the capture host has no
# display server, so we render a real Chromium inside Xvfb and grab the X root
# window with ffmpeg x11grab (see capture-client-decisions.sh).
#
# Usage: start-browser.sh <url>
set -euo pipefail

URL="${1:?url}"
: "${SCREEN_W:=1280}" ; : "${SCREEN_H:=900}"
: "${DISPLAY_NUM:=:99}"
: "${PORT:=9222}"
: "${RUNDIR:=/tmp/termini-capture}"
CHROME="${CHROME:-/home/ubuntu/.hermes/tools/chromium-1208/chrome-linux64/chrome}"
mkdir -p "$RUNDIR"

if ! xdpyinfo -display "$DISPLAY_NUM" >/dev/null 2>&1; then
  nohup Xvfb "$DISPLAY_NUM" -screen 0 "${SCREEN_W}x${SCREEN_H}x24" -nolisten tcp \
    >"$RUNDIR/xvfb.log" 2>&1 &
  echo $! > "$RUNDIR/xvfb.pid"
  for _ in $(seq 1 40); do
    xdpyinfo -display "$DISPLAY_NUM" >/dev/null 2>&1 && break
    sleep 0.25
  done
fi

# Fresh profile per run: no "restore pages?" bubble, no stale localStorage.
rm -rf "$RUNDIR/profile"
mkdir -p "$RUNDIR/profile"
# The evidence run signs in through the dashboard's own Settings dialog (the
# credential is a `password` input there), which makes Chrome offer its
# "Save password?" bubble — a browser overlay across the middle of the frame.
# The password manager is off for this throwaway profile; nothing about the page
# is changed by it.
mkdir -p "$RUNDIR/profile/Default"
printf '%s\n' '{"credentials_enable_service":false,"credentials_enable_autosignin":false,"profile":{"password_manager_enabled":false,"password_manager_leak_detection":false}}' \
  > "$RUNDIR/profile/Default/Preferences"
export DISPLAY="$DISPLAY_NUM"
nohup "$CHROME" \
  --no-sandbox --disable-gpu --disable-dev-shm-usage \
  --no-first-run --no-default-browser-check --disable-infobars \
  --disable-session-crashed-bubble --disable-features=Translate,OptimizationHints \
  --window-size="${SCREEN_W},${SCREEN_H}" --window-position=0,0 \
  --user-data-dir="$RUNDIR/profile" --remote-debugging-port="$PORT" \
  "$URL" >"$RUNDIR/chrome.log" 2>&1 &
echo $! > "$RUNDIR/chrome.pid"

for _ in $(seq 1 60); do
  curl -s -m 2 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1 && break
  sleep 0.5
done
sleep 1
# The window must tile the whole screen, otherwise the grab catches bare root
# window next to it.
xdotool search --onlyvisible --class chrome windowsize "$SCREEN_W" "$SCREEN_H" 2>/dev/null || true
xdotool search --onlyvisible --class chrome windowmove 0 0 2>/dev/null || true
echo "browser up on $DISPLAY_NUM (pid $(cat "$RUNDIR/chrome.pid")), cdp :$PORT, screen ${SCREEN_W}x${SCREEN_H}"
