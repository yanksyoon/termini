#!/usr/bin/env bash
# Render a raw terminal transcript (the train of truth) into a PNG.
#
# The PNG is a *view* of the transcript file: the bytes are wrapped for display
# (`fold -s`, rendering only — the sidecar .txt keeps the original bytes), written
# to a textfile and drawn with ffmpeg's drawtext on a dark background. Nothing is
# retyped, reordered or trimmed. The .txt is what a reader verifies; the PNG is
# the readable form, per the evidence convention on the E2E cards.
#
# ffmpeg is used rather than a headless browser on purpose: this capture host is
# shared with a concurrent client-side capture, and starting Chromium per frame
# proved both slow and memory-hungry (at load average 65 it produced nothing).
#
# Usage: render-terminal-png.sh <transcript.txt> <out.png> [title]
set -euo pipefail

TXT="${1:?transcript}"
PNG="${2:?png}"
TITLE="${3:-$(basename "$TXT")}"
FFMPEG="${FFMPEG:-/home/ubuntu/.hermes/tools/ffmpeg-9.0.1-linux-x64/bin/ffmpeg}"
FONT="${FONT:-/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf}"
COLS="${COLS:-215}"
FONTSIZE="${FONTSIZE:-12}"
LINEH="${LINEH:-16}"

[ -f "$TXT" ] || { echo "no such transcript: $TXT" >&2; exit 1; }
[ -x "$FFMPEG" ] || { echo "no ffmpeg at $FFMPEG" >&2; exit 1; }
[ -f "$FONT" ] || { echo "no font at $FONT" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

SHA="$(sha256sum "$TXT" | cut -d' ' -f1)"
BYTES="$(wc -c <"$TXT")"
LINES="$(wc -l <"$TXT")"

{
  printf 'termini — %s\n' "$TITLE"
  printf 'raw transcript: %s\nsha256 %s · %s bytes · %s lines · rendered %s UTC (output unmodified; lines wrapped for display only)\n' \
    "$TXT" "$SHA" "$BYTES" "$LINES" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '%s\n' "--------------------------------------------------------------------------------"
  fold -s -w "$COLS" "$TXT"
} >"$WORK/page.txt"

N="$(wc -l <"$WORK/page.txt")"
HEIGHT=$(( N * LINEH + 24 ))
[ "$HEIGHT" -lt 400 ] && HEIGHT=400
[ "$HEIGHT" -gt 16000 ] && HEIGHT=16000

# drawtext needs the path escaped for the filter graph ('\' and ':' are special).
TFILE="$(printf '%s' "$WORK/page.txt" | sed 's/\\/\\\\/g; s/:/\\:/g')"

"$FFMPEG" -hide_banner -loglevel error -y \
  -f lavfi -i "color=c=0x0d1117:s=1680x${HEIGHT}" \
  -vf "drawtext=fontfile=${FONT}:textfile=${TFILE}:fontcolor=0xe6edf3:fontsize=${FONTSIZE}:line_spacing=4:expansion=none:x=14:y=12" \
  -frames:v 1 "$PNG"

[ -s "$PNG" ] || { echo "ffmpeg produced no PNG" >&2; exit 1; }
echo "$PNG"
