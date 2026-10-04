#!/usr/bin/env python3
"""Timed dry-run for docs/demo-script.md.

Parses the beat-structured narration script, computes per-beat spoken time
(at a fixed presenter pace) plus explicit [WAIT]/[HOLD] durations plus a small
per-[CLICK] action allowance, and checks the total against the presentation
slot. Prints a table and a verdict. Exit 0 = fits with margin.

Timing model (documented, reproducible):
  - spoken time = narration words / WPM * 60  (default 150 wpm)
  - [WAIT n] / [HOLD n] add n seconds
  - each [CLICK] adds CLICK_SECS (default 2.0)
  - everything else (headings, fallbacks) is not on the live clock
"""
import re
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "docs/demo-script.md"
WPM = 132.6  # measured edge-TTS read-aloud rate in the recorded dry-run (542 words / 245.28 s)
CLICK_SECS = 2.0
SLOT_SECS = 360.0  # 6:00
TARGET_SECS = 300.0  # <= 5:00 target

click_re = re.compile(r"- `\[CLICK\]`")
wait_re = re.compile(r"- `\[WAIT (\d+)\]`")
hold_re = re.compile(r"- `\[HOLD (\d+)\]`")
talk_re = re.compile(r"- `\[TALK\]` \"(.*?)\"", re.S)
beat_re = re.compile(r"^### Beat (\d+) — (.*?)  `\[planned (\d+):(\d+)\]`", re.M)

def words(s):
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*", s))

text = open(PATH).read()
# split into beats
beat_starts = [m.start() for m in beat_re.finditer(text)] + [len(text)]
beats = []
for i, m in enumerate(beat_re.finditer(text)):
    seg = text[beat_starts[i]:beat_starts[i+1]]
    spoken = sum(words(t) for t in talk_re.findall(seg))
    waits = sum(int(x) for x in wait_re.findall(seg))
    holds = sum(int(x) for x in hold_re.findall(seg))
    clicks = len(click_re.findall(seg))
    planned_m, planned_s = int(m.group(3)), int(m.group(4))
    planned = planned_m * 60 + planned_s
    spoken_secs = spoken / WPM * 60.0
    act_secs = waits + holds + clicks * CLICK_SECS
    beats.append({
        "n": int(m.group(1)),
        "title": m.group(2),
        "planned": planned,
        "words": spoken,
        "spoken": spoken_secs,
        "act": act_secs,
        "total": spoken_secs + act_secs,
    })

print(f"{'Beat':<5} {'planned':>8} {'words':>5} {'spoken':>7} {'actions':>8} {'actual':>7} {'delta':>7}")
total_planned = 0
total_actual = 0
total_act = 0
total_spoken = 0
for b in beats:
    d = b["total"] - b["planned"]
    total_planned += b["planned"]
    total_actual += b["total"]
    total_act += b["act"]
    total_spoken += b["spoken"]
    print(f"{b['n']:<5} {b['planned']:>7}s {b['words']:>5} {b['spoken']:>6.1f}s {b['act']:>7.1f}s {b['total']:>6.1f}s {d:>+6.1f}s")
print("-" * 60)
print(f"{'TOTAL':<5} {total_planned:>7}s {'':>5} {total_spoken:>6.1f}s {total_act:>7.1f}s {total_actual:>6.1f}s {total_actual-total_planned:>+6.1f}s")

def fmt(s):
    s = round(s)
    return f"{s//60}:{s%60:02d}"

print()
print(f"Slot: {fmt(SLOT_SECS)}  Target (<=): {fmt(TARGET_SECS)}  Actual total: {fmt(total_actual)}")
margin = SLOT_SECS - total_actual
print(f"Margin under slot: {fmt(margin)} ({margin:.0f}s, {margin/SLOT_SECS*100:.0f}%)")
ok = total_actual <= TARGET_SECS
print(f"Verdict: {'FITS with margin' if ok else 'OVER target — trim required'}")
sys.exit(0 if ok else 1)
