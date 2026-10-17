#!/usr/bin/env python3
"""Incidents master–detail: prove the click, and prove the record survives a poll.

    python3 tools/selection_check.py --root ../../            # serve the build here
    python3 tools/selection_check.py --base http://127.0.0.1:8110

Regression check for t_0da3f8b2. Two defects, both measured on the real page:

  1. every decision row looked clickable — pointer cursor, hover/pressed
     feedback, `role="option"` — and clicking one changed nothing;
  2. the only working path, the keyboard, usually rendered an EMPTY pane: the
     selected decision's four body parts were sitting parked in `.hig-store`
     with `data-hig-part="<id>"` and were never restored, because a rebuilt row
     is a row identity and the poll replaces row elements.

Steps, all driven through CDP so they are what a reader does:

  1. load `/?offline=1&dest=incidents` and wait for the list;
  2. click row 2 with a real mouse event: the selection and the pane must both
     move to that decision, exactly one row carries `aria-selected="true"`, and
     focus must be on it (so ArrowDown continues from there);
  3. press ArrowDown with a real key event: the pane must follow;
  4. wait 12 s (4 decision polls): the pane must still hold the same decision,
     with one of each block — no duplicated card, no lost disclosure;
  5. write one screenshot of the clicked record to `evidence/hig/shots/`.

Exit status is 0 only when every assertion holds; each line prints what was
measured, so the pre-fix run is readable evidence too.
"""
import argparse
import base64
import functools
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
CHROME = os.environ.get("CHROME", os.path.expanduser("~/.hermes/tools/chromium-1208/chrome-linux64/chrome"))
import cdp  # noqa: E402

SIZE = (1440, 1200)


def free_port():
    """A port nobody else holds — a stale browser on the fixed port is a trap:
    `connect()` would attach to it and measure somebody else's page."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_chrome(port, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                return json.load(r)
        except Exception:
            time.sleep(0.2)
    raise SystemExit("chrome did not expose CDP")


def launch():
    port = free_port()
    profile = tempfile.mkdtemp(prefix="hig-select-", dir=os.environ.get("TMPDIR") or None)
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            # A loaded host (other agents' browsers, the inference server) will
            # starve a default Chromium: its network service crashes on startup
            # and CDP never answers. One renderer, no site isolation, no
            # background networking keeps this check inside a few hundred MB.
            "--renderer-process-limit=1",
            "--disable-site-isolation-trials",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-features=Translate,MediaRouter,OptimizationHints",
            "--js-flags=--max-old-space-size=256",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        cdp.PORT = port
        return proc, wait_for_chrome(port)
    except BaseException:
        proc.terminate()  # never leave a browser behind when the launch fails
        raise


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # a check prints its own output only
        pass


def serve(root):
    """A static server for the build under test (no deploy, no network)."""
    handler = functools.partial(QuietHandler, directory=os.path.abspath(root))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


STATE = r"""
(() => {
  const rows = [...document.querySelectorAll('#decision-list > .decision-row')];
  const pane = document.getElementById('hig-incident-detail');
  const store = document.querySelector('section.hig-view[data-view="incidents"] .hig-store')
             || document.querySelector('.hig-store');
  if (!pane) return { error: 'no #hig-incident-detail' };
  const selected = rows.filter((r) => r.getAttribute('aria-selected') === 'true');
  const parts = [...pane.children].filter((n) => n.dataset && n.dataset.higPart);
  const text = (pane.innerText || '').replace(/\s+/g, ' ').trim();
  const storeParts = store ? [...store.children].filter((n) => n.dataset && n.dataset.higPart) : [];
  const storeIds = storeParts.map((n) => n.dataset.higPart);
  const title = pane.querySelector('.incident-title');
  // A block that appears twice for the SAME decision is a duplicate; several
  // parts per decision (card, trace, breach, rationale, links) are normal.
  const seen = new Set();
  let duplicates = 0;
  for (const n of [...parts, ...storeParts]) {
    const key = n.dataset.higPart + '|' + ((typeof n.className === 'string' ? n.className : '').split(/\s+/)[0] || '');
    if (seen.has(key)) duplicates += 1;
    else seen.add(key);
  }
  return {
    rows: rows.length,
    selectedId: selected.length === 1 ? selected[0].dataset.decisionId : null,
    selectedCount: selected.length,
    tab0: rows.filter((r) => r.tabIndex === 0).map((r) => r.dataset.decisionId),
    firstId: rows.length ? rows[0].dataset.decisionId : null,
    secondId: rows.length > 1 ? rows[1].dataset.decisionId : null,
    panePartIds: [...new Set(parts.map((n) => n.dataset.higPart))],
    paneTextLen: text.length,
    paneText: text.slice(0, 200),
    paneTitle: title ? title.textContent.trim() : null,
    paneCards: pane.querySelectorAll('.incident-card').length,
    paneNoRecord: pane.querySelectorAll('.incident-none').length,
    paneLinks: [...pane.querySelectorAll('.decision-links button, .decision-links a')].map((n) => n.textContent.trim()),
    paneTrace: pane.querySelectorAll('.incident-trace, .trace-none').length,
    paneDetails: pane.querySelectorAll('details.trace-details').length,
    paneButtons: pane.querySelectorAll('button').length,
    paneDisabled: pane.querySelectorAll('button[disabled]').length,
    rowControls: rows.reduce((n, r) => n + r.querySelectorAll('button, a, summary, details').length, 0),
    storeCards: storeParts.filter((n) => n.classList.contains('incident-card')).length,
    storeParts: storeParts.length,
    storeUniqueIds: new Set(storeIds).size,
    storeHasSelected: storeIds.includes(selected.length === 1 ? selected[0].dataset.decisionId : '-'),
    duplicates,
    openDetails: [...pane.querySelectorAll('details')].filter((d) => d.open).length,
    activeId: document.activeElement && document.activeElement.dataset
      ? document.activeElement.dataset.decisionId || null : null,
    emptyHidden: (document.getElementById('hig-incident-empty') || {}).hidden,
    noBodyHidden: (document.getElementById('hig-incident-no-body') || {}).hidden,
  };
})()
"""


def state(ws):
    return cdp.evaluate(ws, STATE)


def wait_state(ws, predicate, timeout=25):
    """Poll the page until it is ready — a fixed sleep races the first payload."""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = state(ws)
        if not last.get("error") and predicate(last):
            return last
        time.sleep(0.4)
    return last


def stable(ws, timeout=12):
    """The offline stream appends a decision every few seconds and the list
    re-orders under the pointer. Click in a moment when it has not moved."""
    prev = state(ws)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.35)
        cur = state(ws)
        if cur.get("firstId") == prev.get("firstId") and cur.get("rows") == prev.get("rows"):
            return cur
        prev = cur
    return prev


def el_box(ws, selector):
    return cdp.evaluate(
        ws,
        "(() => { const n = document.querySelector(%s); if (!n) return null;"
        " n.scrollIntoView({block: 'center'}); const b = n.getBoundingClientRect();"
        " return { x: Math.round(b.left + b.width / 2), y: Math.round(b.top + b.height / 2) }; })()"
        % json.dumps(selector),
    )


PICK_JS = r"""
(() => {
  const rows = [...document.querySelectorAll('#decision-list > .decision-row')];
  const hosts = [document.getElementById('hig-incident-detail'), document.querySelector('.hig-store')];
  const hasCard = new Set(), hasDetails = new Set();
  for (const host of hosts) {
    if (!host) continue;
    for (const n of [...host.children]) {
      const id = n.dataset && n.dataset.higPart;
      if (!id) continue;
      if (n.classList.contains('incident-card')) hasCard.add(id);
      if (n.querySelector('details')) hasDetails.add(id);
    }
  }
  // A record with a real incident card AND a thinking-log disclosure, not the
  // newest decision (which is already selected): that is the demo moment.
  const candidates = [];
  for (let i = 1; i < rows.length; i++) {
    const id = rows[i].dataset.decisionId;
    if (hasCard.has(id) && hasDetails.has(id)) candidates.push(i);
  }
  const index = candidates.length ? candidates[0] : rows.length > 1 ? 1 : null;
  if (index === null) return null;
  rows[index].scrollIntoView({ block: 'center' });
  const b = rows[index].getBoundingClientRect();
  if (b.width < 20 || b.height < 8) return null;
  return {
    index,
    id: rows[index].dataset.decisionId,
    x: Math.round(b.left + Math.min(120, b.width / 2)),
    y: Math.round(b.top + b.height / 2),
  };
})()
"""


def rich_row(ws):
    return cdp.evaluate(ws, PICK_JS)


def row_box(ws, index):
    return cdp.evaluate(
        ws,
        "(() => { const rows = [...document.querySelectorAll('#decision-list > .decision-row')];"
        f" const r = rows[{index}]; if (!r) return null; const b = r.getBoundingClientRect();"
        " return { x: Math.round(b.left + Math.min(120, b.width / 2)), y: Math.round(b.top + b.height / 2),"
        " id: r.dataset.decisionId }; })()",
    )


def click_xy(ws, x, y):
    for kind in ("mousePressed", "mouseReleased"):
        ws.call(
            "Input.dispatchMouseEvent",
            {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1, "buttons": 1},
        )
        time.sleep(0.05)


def press(ws, key, vk):
    for kind in ("keyDown", "keyUp"):
        ws.call(
            "Input.dispatchKeyEvent",
            {"type": kind, "key": key, "code": key, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk},
        )
        time.sleep(0.05)


class Check:
    def __init__(self):
        self.failures = []
        self.lines = []

    def ok(self, name, condition, detail):
        self.lines.append({"check": name, "ok": bool(condition), "detail": detail})
        print(("PASS  " if condition else "FAIL  ") + name + " — " + detail)
        if not condition:
            self.failures.append(name)
        return condition


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None, help="already-running base URL")
    ap.add_argument("--root", default=None, help="serve this directory (dashboard/) over http")
    ap.add_argument("--out", default=os.path.dirname(HERE), help="evidence dir (default: evidence/hig)")
    ap.add_argument("--wait", type=int, default=12, help="seconds to sit through polls (default 12)")
    ap.add_argument("--tag", default="13-incidents-clicked-record-1440x1200-dark")
    args = ap.parse_args()

    httpd = None
    base = args.base
    if not base:
        root = args.root or os.path.join(HERE, "..", "..", "..")  # dashboard/
        httpd, base = serve(root)
    print(f"base: {base}")

    outdir = os.path.abspath(args.out)
    os.makedirs(os.path.join(outdir, "shots"), exist_ok=True)
    os.makedirs(os.path.join(outdir, "audit"), exist_ok=True)
    check = Check()

    proc, info = launch()
    try:
        ws = cdp.connect()
        cdp.apply_size(ws, f"{SIZE[0]}x{SIZE[1]}", False)
        cdp.apply_media(ws, "dark")
        ws.call("Page.navigate", {"url": f"{base}/?offline=1&dest=incidents"})
        first = wait_state(ws, lambda s: s.get("rows", 0) > 2 and s.get("paneTextLen", 0) > 0)
        if first.get("error"):
            raise SystemExit(first["error"])
        check.ok(
            "the incidents list rendered",
            first["rows"] > 2 and first["paneTextLen"] > 0,
            f"{first['rows']} rows, first decision selected, pane {first['paneTextLen']} chars",
        )
        check.ok(
            "rows are rows: no control nested in an option",
            first["rowControls"] == 0,
            f"{first['rowControls']} button/a/summary/details inside the list rows",
        )

        box = None
        before = first
        after = first
        for attempt in range(1, 6):
            before = stable(ws)
            box = rich_row(ws) or row_box(ws, 3) or row_box(ws, 2)
            if not box:
                break
            click_xy(ws, box["x"], box["y"])
            time.sleep(0.6)
            after = state(ws)
            if after["selectedId"] == box["id"] and after["selectedId"] != before["selectedId"]:
                break
            reordered = after.get("firstId") != before.get("firstId") or after.get("rows") != before.get("rows")
            if not reordered:
                print(f"  (the list did not move: a click on {box['id']} left the selection on "
                      f"{after['selectedId']})")
                break
            print(f"  (attempt {attempt}: the offline stream re-ordered the list under the pointer — "
                  f"aimed at {box['id']}, selection went to {after['selectedId']})")

        check.ok("a row has a box to click", bool(box), json.dumps(box))
        check.ok(
            "a click on that row selects that row",
            after["selectedId"] == box["id"] and after["selectedCount"] == 1
            and after["selectedId"] != before["selectedId"],
            f"aria-selected moved {before['selectedId']} -> {after['selectedId']} (clicked {box['id']}), "
            f"{after['selectedCount']} row selected",
        )
        check.ok(
            "the click moves focus, so ArrowDown continues here",
            after["activeId"] == after["selectedId"] and after["tab0"] == [after["selectedId"]],
            f"activeElement decision {after['activeId']}, tabIndex 0 on {after['tab0']}",
        )
        check.ok(
            "the pane renders that decision's record (never blank)",
            after["paneTextLen"] > 0 and after["panePartIds"] == [after["selectedId"]],
            f"pane {after['paneTextLen']} chars, parts {after['panePartIds']}, "
            f"title {after['paneTitle']!r}",
        )
        check.ok(
            "the record pane moved, not just the highlight",
            after["paneText"] != before["paneText"],
            f"pane text changed: {before['paneTitle']!r} -> {after['paneTitle']!r}",
        )
        check.ok(
            "the full record is there: card, thinking log, breach, rationale, links",
            after["paneCards"] + after["paneNoRecord"] >= 1
            and after["paneTrace"] >= 1
            and len(after["paneLinks"]) >= 3,
            f"cards {after['paneCards']}, no-record {after['paneNoRecord']}, traces {after['paneTrace']}, "
            f"links {after['paneLinks']}",
        )
        check.ok(
            "the record's own controls stay usable",
            after["paneButtons"] > 0 and after["paneDisabled"] == 0,
            f"{after['paneButtons']} buttons in the pane, {after['paneDisabled']} disabled",
        )
        check.ok(
            "the record the reader left is parked, and nothing is duplicated",
            after["storeHasSelected"] is False
            and after["duplicates"] == 0
            and before["selectedId"] not in after["panePartIds"],
            f"store holds {after['storeParts']} parts over {after['storeUniqueIds']} decisions, "
            f"duplicate blocks {after['duplicates']}, selected id still in the store: {after['storeHasSelected']}",
        )

        # The demo moment, from the top of the record.
        cdp.evaluate(
            ws,
            "(() => { const p = document.getElementById('hig-incident-detail'); if (p) p.scrollTop = 0;"
            " return p ? p.scrollTop : null; })()",
        )
        time.sleep(0.2)
        shot = os.path.join(outdir, "shots", args.tag + ".png")
        msg = ws.call("Page.captureScreenshot", {"format": "png"})
        with open(shot, "wb") as fh:
            fh.write(base64.b64decode(msg["result"]["data"]))
        print(f"screenshot: {shot} ({os.path.getsize(shot)} bytes)")

        # The keyboard shares the one selection state — and it starts from the
        # row the pointer focused, which is the point of moving focus on click.
        press(ws, "ArrowDown", 40)
        time.sleep(0.5)
        keyed = state(ws)
        check.ok(
            "ArrowDown continues from the clicked row",
            keyed["selectedId"] not in (None, after["selectedId"])
            and keyed["panePartIds"] == [keyed["selectedId"]]
            and keyed["paneTextLen"] > 0
            and keyed["selectedCount"] == 1,
            f"selection {after['selectedId']} -> {keyed['selectedId']}, pane parts {keyed['panePartIds']}, "
            f"{keyed['paneTextLen']} chars",
        )
        press(ws, "ArrowUp", 38)
        time.sleep(0.5)
        restored = state(ws)
        check.ok(
            "ArrowUp returns to the row the pointer selected",
            restored["selectedId"] == after["selectedId"],
            f"selection {keyed['selectedId']} -> {restored['selectedId']}",
        )

        # Open the thinking log, so the poll below has a disclosure to lose.
        summary = el_box(ws, "#hig-incident-detail details > summary")
        opened = restored
        if summary:
            click_xy(ws, summary["x"], summary["y"])
            time.sleep(0.4)
            opened = state(ws)
        check.ok(
            "the thinking log opens inside the record",
            bool(summary) and opened["openDetails"] >= 1,
            f"{opened['openDetails']} open disclosure(s) in the pane after a click on the summary"
            if summary else "no disclosure in this record",
        )

        # Sit through four decision polls (3 s each) with the record selected.
        time.sleep(args.wait)
        settled = state(ws)
        check.ok(
            "the record survives the 3 s poll",
            settled["selectedId"] == after["selectedId"]
            and settled["panePartIds"] == [after["selectedId"]]
            and settled["paneTextLen"] > 0,
            f"after {args.wait}s: selection {settled['selectedId']}, pane parts {settled['panePartIds']}, "
            f"{settled['paneTextLen']} chars, title {settled['paneTitle']!r}",
        )
        check.ok(
            "no duplicated block, no lost disclosure",
            settled["duplicates"] == 0
            and settled["paneCards"] <= 1
            and settled["paneTrace"] <= 1
            and settled["openDetails"] >= opened["openDetails"]
            and settled["rowControls"] == 0,
            f"duplicate blocks {settled['duplicates']}, cards {settled['paneCards']}, "
            f"traces {settled['paneTrace']}, open disclosures {settled['openDetails']} "
            f"(was {opened['openDetails']}), row controls {settled['rowControls']}",
        )

        result = {
            "browser": info.get("Browser"),
            "url": cdp.evaluate(ws, "location.href"),
            "viewport": list(SIZE),
            "checks": check.lines,
            "pass": not check.failures,
            "first": first,
            "clicked": after,
            "keyboard": keyed,
            "settled": settled,
            "screenshot": os.path.relpath(shot, os.path.dirname(outdir)),
        }
        with open(os.path.join(outdir, "audit", "selection.json"), "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, ensure_ascii=False)
        print("ACCEPTANCE:", "PASS" if result["pass"] else "FAIL (" + ", ".join(check.failures) + ")")
        raise SystemExit(0 if result["pass"] else 1)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if httpd:
            httpd.shutdown()


if __name__ == "__main__":
    main()
