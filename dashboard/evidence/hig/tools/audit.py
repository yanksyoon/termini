#!/usr/bin/env python3
"""Run the HIG acceptance measurements against one destination/appearance.

    python3 tools/audit.py <out.json> --url <url> [--size 1440x900] [--mobile]
                           [--media dark|light] [--wait 3500]

Writes the raw measurement object plus a printed summary: viewport fit, chrome
control count, targets under 44 px, unlabelled icon-only controls, the type
scale and weights, and every text run below its contrast requirement.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--url", required=True)
    ap.add_argument("--size", default=None)
    ap.add_argument("--mobile", action="store_true")
    ap.add_argument("--media", default=None)
    ap.add_argument("--wait", type=int, default=3500)
    args = ap.parse_args()

    ws = cdp.connect()
    if args.size:
        cdp.apply_size(ws, args.size, args.mobile)
    if args.media:
        cdp.apply_media(ws, args.media)
    ws.call("Page.navigate", {"url": args.url})
    time.sleep(args.wait / 1000.0)

    expr = open(os.path.join(HERE, "audit_measure.js"), encoding="utf-8").read()
    result = cdp.evaluate(ws, expr)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)

    sizes = sorted(result["fontSizes"].items(), key=lambda kv: float(kv[0]))
    weights = sorted(result["fontWeights"].items())
    print(f"url            {result['url']}")
    print(f"variant        {result['variant']} / {result['destination']} / {result['appearance']} (dark={result['prefersDark']})")
    print(f"viewport       {result['viewport']['w']}x{result['viewport']['h']}  doc {result['doc']['scrollWidth']}x{result['doc']['scrollHeight']}")
    print(f"h-overflow     {result['horizontalOverflow']}")
    print(f"controls       {len(result['controls'])} visible, chrome {result['chromeCount']}, under44 {len(result['under44'])}")
    for c in result["under44"][:12]:
        print(f"  under44      {c['h']}x{c['w']} {c['tag']} '{c['label']}' ({c['sel']})")
    print(f"icon-only w/o label  {len(result['iconOnlyUnlabelled'])}")
    for c in result["iconOnlyUnlabelled"][:8]:
        print(f"  unlabelled   {c['tag']} ({c['sel']})")
    print(f"font sizes     {sizes}")
    print(f"font weights   {weights}")
    print(f"text colours   {len(result['textColors'])}")
    for col, n in sorted(result["textColors"].items(), key=lambda kv: -kv[1]):
        print(f"  {col}  x{n}")
    print(f"contrast runs  {result['contrastChecked']} checked, {len(result['contrastWorst'])} below target")
    for row in result["contrastWorst"][:15]:
        print(f"  {row['ratio']:>5} (need {row['need']}) {row['size']}px {row['text']!r} {row['color']} on {row['bg']}")
    print(f"destinations   {result['destinations']}")
    print(f"listbox        {result['listboxes']} listbox(es), {result['options']} options, {result['selectedOptions']} selected")


if __name__ == "__main__":
    main()
