#!/usr/bin/env python3
"""Print the series the chart drew, in the chart's own buckets, and check the fault.

Why: a PNG of a canvas proves nothing on its own — the pixels are a rendering of a
metric payload. This prints that payload as the numbers behind the line (one row
per 5 s bucket, the resolution the dashboard requests for a 15 m window) plus the
threshold the rule fires at, so a reader can check "rose above, came back" without
trusting the picture.

Usage:
  series.py <metrics.json> [--metric ram_used_bytes] [--threshold-pct 95]

Exit code 0 when the series both went above the threshold and came back below it,
1 otherwise (so a capture script can refuse to shoot a frame that does not prove
the fault), 2 on a usage/shape problem.
"""
import argparse
import json
import sys

GIB = 1024.0 ** 3


def load(path):
    with open(path) as fh:
        return json.load(fh)


def series_map(doc):
    out = {}
    for s in doc.get("series") or []:
        if isinstance(s, dict) and isinstance(s.get("name"), str):
            out[s["name"]] = s.get("points") or []
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--metric", default="ram_used_bytes")
    ap.add_argument("--threshold-pct", type=float, default=95.0)
    args = ap.parse_args()

    doc = load(args.path)
    sm = series_map(doc)
    points = sm.get(args.metric)
    if not points:
        print(f"# no {args.metric} series in {args.path} (names: {sorted(sm)})")
        return 2

    total = None
    for p in sm.get("ram_total_bytes") or []:
        if isinstance(p.get("v"), (int, float)) and p["v"] > 0:
            total = float(p["v"])
            break
    if total is None:
        print("# no ram_total_bytes series — cannot express the threshold")
        return 2
    threshold = total * args.threshold_pct / 100.0

    print(f"# source: {args.path}")
    print(f"# node {doc.get('node_id')}  resolution {doc.get('resolution')}  "
          f"buckets {len(points)}")
    print(f"# ram_total_bytes {int(total)} ({total / GIB:.2f} GiB)")
    print(f"# threshold {args.threshold_pct:.1f}% of ram_total_bytes = "
          f"{int(threshold)} bytes ({threshold / GIB:.2f} GiB)")
    print("#")
    print(f"{'bucket (UTC)':<22} {'bytes':>14} {'GiB':>7} {'% total':>8}  flag")

    above = []
    for i, p in enumerate(points):
        v = p.get("v")
        if not isinstance(v, (int, float)):
            continue
        pct = v / total * 100.0
        flag = "*" if v >= threshold else ""
        if flag:
            above.append(i)
        print(f"{p.get('t'):<22} {int(v):>14} {v / GIB:>7.2f} {pct:>7.2f}%  {flag}")

    print()
    if not above:
        print("RESULT: never reached the threshold — this series does NOT prove a breach")
        return 1
    first, last = above[0], above[-1]
    below_after = any(
        isinstance(p.get("v"), (int, float)) and p["v"] < threshold
        for p in points[last + 1:]
    )
    print(f"RESULT: {len(above)} bucket(s) at or above the threshold; "
          f"first {points[first].get('t')} (*), last {points[last].get('t')} (*)")
    print(f"        returned below the threshold after the peak: {below_after}")
    if not below_after:
        print("        NO RECOVERY in this window — not a rise-and-recover frame")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
