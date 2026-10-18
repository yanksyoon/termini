#!/usr/bin/env bash
# Grab the whole X root window (headful Chromium inside Xvfb) to a PNG.
# Usage: shot.sh <out.png> [display]
set -euo pipefail
OUT="${1:?out.png}"
DISP="${2:-:99}"
FFMPEG="${FFMPEG:-/home/ubuntu/.hermes/tools/ffmpeg-9.0.1-linux-x64/bin/ffmpeg}"
mkdir -p "$(dirname "$OUT")"
"$FFMPEG" -hide_banner -loglevel error -y -f x11grab -video_size "${SHOT_W:-1280}x${SHOT_H:-1000}" \
  -i "$DISP.0" -frames:v 1 "$OUT"
ls -l "$OUT" | awk '{print $5, $9}'
