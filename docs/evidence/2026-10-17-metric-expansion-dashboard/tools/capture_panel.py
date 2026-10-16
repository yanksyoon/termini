#!/usr/bin/env python3
"""Capture the dashboard's metric-expansion panel out of a real browser.

Runs against a dashboard page the harness has already served and a controller it
has already fed (see `run-capture.sh`). It seeds the tab's configuration (the
project credential goes into `sessionStorage`, never into a URL — the rule the
dashboard enforces), reloads, WAITS for the §4.12–§4.14 cards to exist, and then
writes, per state:

    <prefix>-panel.txt    the panel's own rendered text (innerText, read out of the DOM)
    <prefix>-charts.png   the #charts element, as rendered
    <prefix>-viewport.png the whole viewport (URL visible in the address bar)

It also ASSERTS on the text: `--expect` must be present and `--forbid` must be
absent, so a capture that silently produced an empty panel exits non-zero instead
of committing blank evidence.

Usage:
    capture_panel.py --port 9222 --api-base http://127.0.0.1:PORT/v1 \\
        --project-id 01... --project-token T --capture-dir DIR --prefix 01-real-sample \\
        --expect "OOM kills" --forbid "not reported"
"""
from __future__ import annotations

import argparse
import base64
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cdp import WS, evaluate, pick_page  # noqa: E402

WAIT_JS = """(() => new Promise((res, rej) => {
  const want = %d;
  const t0 = Date.now();
  const iv = setInterval(() => {
    const got = document.querySelectorAll('.sample-card').length;
    if (got >= want) { clearInterval(iv); res({ cards: got, ms: Date.now() - t0 }); }
    else if (Date.now() - t0 > 45000) {
      clearInterval(iv);
      rej(new Error('only ' + got + ' of ' + want + ' sample cards after 45s'));
    }
  }, 250);
}))()"""

TEXT_JS = """(() => {
  const cards = [...document.querySelectorAll('.sample-card')];
  const flat = [...document.querySelectorAll('.chart-card')]
    .filter((c) => !c.classList.contains('sample-card'))
    .map((c) => c.innerText);
  return {
    sample_cards: cards.length,
    panel: cards.map((c) => c.innerText).join('\\n\\n'),
    flat_chart_captions: flat,
    url: location.href,
  };
})()"""

BOX_JS = """(() => {
  const el = document.querySelector('#charts');
  if (!el) return null;
  const r = el.getBoundingClientRect();
  // Page coordinates: Page.captureScreenshot's clip is relative to the DOCUMENT,
  // while getBoundingClientRect is relative to the viewport.
  return { x: r.x + window.scrollX, y: r.y + window.scrollY, width: r.width, height: r.height };
})()"""

SCROLL_JS = """(() => {
  const el = document.querySelector('.sample-card');
  if (el) el.scrollIntoView({ block: 'start' });
  return el ? el.getBoundingClientRect().y : null;
})()"""


def shot(ws, out: pathlib.Path, clip=None, beyond=False):
    params = {"format": "png", "captureBeyondViewport": bool(beyond)}
    if clip:
        params["clip"] = {**clip, "scale": 1}
    msg = ws.call("Page.captureScreenshot", params)
    data = (msg.get("result") or {}).get("data")
    if not data:
        raise SystemExit(f"Page.captureScreenshot returned nothing: {json.dumps(msg)[:300]}")
    out.write_bytes(base64.b64decode(data))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", required=True)
    ap.add_argument("--api-base", required=True)
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--project-token", required=True)
    ap.add_argument("--capture-dir", required=True)
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--want-cards", type=int, default=6)
    ap.add_argument("--expect", action="append", default=[])
    ap.add_argument("--forbid", action="append", default=[])
    args = ap.parse_args()

    out = pathlib.Path(args.capture_dir)
    out.mkdir(parents=True, exist_ok=True)
    ws = WS(pick_page(args.port)["webSocketDebuggerUrl"])
    ws.call("Page.enable")

    seed = (
        "(() => {"
        f"localStorage.setItem('termini.dashboard.cfg', {json.dumps(json.dumps({'apiBase': args.api_base, 'projectId': args.project_id, 'offline': False}))});"
        f"sessionStorage.setItem('termini.dashboard.token', {json.dumps(args.project_token)});"
        "sessionStorage.removeItem('termini.dashboard.node');"
        "return true; })()"
    )
    res = evaluate(ws, seed)
    if "value" not in res:
        raise SystemExit(f"could not seed the tab config: {json.dumps(res)}")

    ws.call("Page.reload", {"ignoreCache": True})
    time.sleep(1.0)
    waited = evaluate(ws, WAIT_JS % args.want_cards)
    if "value" not in waited:
        raise SystemExit(f"the panel never rendered {args.want_cards} sample cards: {json.dumps(waited)[:400]}")

    data = evaluate(ws, TEXT_JS)
    if "value" not in data or not isinstance(data["value"], dict):
        raise SystemExit(f"could not read the panel text: {json.dumps(data)[:400]}")
    payload = data["value"]
    payload["waited"] = waited["value"]
    payload["captured_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    (out / f"{args.prefix}-panel.txt").write_text(payload["panel"] + "\n")
    (out / f"{args.prefix}-panel.json").write_text(json.dumps(payload, indent=2) + "\n")

    box = evaluate(ws, BOX_JS).get("value")
    shot(ws, out / f"{args.prefix}-viewport.png")
    if box and box.get("width") and box.get("height"):
        # The panel is taller than the viewport, so this one is captured beyond it.
        shot(ws, out / f"{args.prefix}-charts.png", clip=box, beyond=True)
    # Leave the page scrolled to the new panel: the caller's X11 grab (which shows
    # the browser chrome, and therefore the URL) then frames the same content.
    evaluate(ws, SCROLL_JS)
    time.sleep(0.6)

    problems = []
    text = payload["panel"]
    for needle in args.expect:
        if needle not in text:
            problems.append(f"expected {needle!r} in the panel text")
    for needle in args.forbid:
        if needle in text:
            problems.append(f"{needle!r} must NOT be in the panel text")
    if problems:
        print(json.dumps({"prefix": args.prefix, "problems": problems,
                          "url": payload.get("url")}, indent=2), file=sys.stderr)
        return 1
    print(json.dumps({"prefix": args.prefix, "url": payload.get("url"),
                      "sample_cards": payload["sample_cards"],
                      "waited": payload["waited"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
