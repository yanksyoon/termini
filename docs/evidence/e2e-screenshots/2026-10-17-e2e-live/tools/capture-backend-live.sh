#!/usr/bin/env bash
# Re-runnable capture driver for the BACKEND half of the E2E evidence pack
# (kanban card t_2191eca4).
#
#   docs/evidence/e2e-screenshots/<run-id>/backend/NN-<step-slug>.png   the readable form
#   docs/evidence/e2e-screenshots/<run-id>/backend/NN-<step-slug>.txt   the raw transcript (train of truth)
#
# Each step is a snippet under tools/steps/. The snippet is executed through
# `script -qec` so the transcript is exactly what the terminal produced; the
# only transformation applied afterwards is stripping the CR that the PTY adds
# to every line (recorded in the index). The PNG is rendered from that
# transcript by tools/render-terminal-png.sh.
#
# Usage:
#   bash tools/capture-backend-live.sh              # every step, in order
#   bash tools/capture-backend-live.sh 03 04        # only these steps (re-capture)
#
# Environment it ran against is recorded in tools/environment.md and in the
# index; costs: this script never creates or starts GCP compute (the fleet must
# already be up — see the card's cost guard).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACK="$(cd "$HERE/.." && pwd)"
OUT="$PACK/backend"
LOG="$PACK/tools/capture-log.tsv"
STATE="${STATE:-/tmp/termini-e2e-backend/state.env}"
mkdir -p "$OUT" /tmp/termini-e2e-backend
# Keep the step-to-step state file across invocations: a re-capture of step 10 must
# still see the decision id that step 08 recorded.
[ -f "$STATE" ] || : >"$STATE"

export API="${API:-https://34-93-190-64.sslip.io/v1}"
export PROJ="${PROJ:-01J0DEM0WARM000000000000AB}"
export TOKEN="${TOKEN:-demo-project-token}"
export STATE

[ -f "$LOG" ] || printf 'step\tslug\tcaptured_at_utc\tstatus\tcommand\ttranscript\tsha256_txt\tsha256_png\tpng\n' >"$LOG"

steps=("$@")
if [ ${#steps[@]} -eq 0 ]; then
  mapfile -t files < <(ls "$HERE"/steps/[0-9][0-9]-*.sh | sort)
else
  files=()
  for s in "${steps[@]}"; do
    f=$(ls "$HERE"/steps/"$s"-*.sh 2>/dev/null | head -1)
    [ -n "$f" ] || { echo "no snippet for step $s" >&2; exit 2; }
    files+=("$f")
  done
fi

for f in "${files[@]}"; do
  base="$(basename "$f" .sh)"
  txt="$OUT/$base.txt"
  raw="$OUT/.$base.raw"
  png="$OUT/$base.png"
  started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "=== $base  (started $started)"

  script -qec "timeout 540 bash '$f'" "$raw" >/dev/null 2>&1
  rc=$?
  # The PTY writes CRLF; strip only the CR (no other byte is touched).
  tr -d '\r' <"$raw" >"$txt"
  rm -f "$raw"

  if [ -s "$txt" ]; then
    bash "$HERE/render-terminal-png.sh" "$txt" "$png" "$base" >/dev/null || echo "  WARN: render failed for $base" >&2
  fi

  status="ok"; [ "$rc" -ne 0 ] && status="exit=$rc"
  cmd=$(sed -n 's/^# CMD: //p' "$f" | head -1)
  sha_txt=$(sha256sum "$txt" | cut -d' ' -f1)
  sha_png=$(sha256sum "$png" 2>/dev/null | cut -d' ' -f1)
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "${base%%-*}" "${base#*-}" "$started" "$status" "$cmd" \
    "backend/$base.txt" "$sha_txt" "${sha_png:-MISSING}" "backend/$base.png" >>"$LOG"
  echo "  $status  $(wc -l <"$txt") lines  ${sha_txt:0:12}"
done

echo "=== done; log: $LOG"
