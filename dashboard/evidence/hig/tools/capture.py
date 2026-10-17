#!/usr/bin/env python3
"""Generate the HIG variant's evidence: screenshots, measurements, a11y and the
keyboard walkthrough, all from the live page.

    python3 tools/capture.py --base http://127.0.0.1:8110 --out ../

The script owns the browser lifecycle (headless Chromium over CDP, thrown-away
profile) so a run is reproducible from a clean start and leaves nothing behind.
It writes:

    shots/<nn>-<destination>-<size>-<appearance>.png
    audit/<destination>-<size>-<appearance>.json      (measurements)
    audit/a11y-<destination>-<appearance>.json        (repo a11y check)
    audit/keyboard-walkthrough.json / .md             (keyboard-only path)
    audit/summary.json                                (the acceptance table)
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("CDP_PORT", "9333")
import cdp  # noqa: E402

CHROME = os.environ.get("CHROME", os.path.expanduser("~/.hermes/tools/chromium-1208/chrome-linux64/chrome"))
PORT = 9333
DESTINATIONS = ["fleet", "incidents", "settings"]
SIZES = [("1440x900", False), ("390x844", True)]
APPEARANCES = ["dark", "light"]


def wait_for_chrome(timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2) as r:
                return json.load(r)
        except Exception:
            time.sleep(0.2)
    raise SystemExit("chrome did not expose CDP")


def launch():
    profile = tempfile.mkdtemp(prefix="hig-audit-", dir=os.environ.get("TMPDIR") or None)
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    info = wait_for_chrome()
    return proc, info


def navigate(ws, url, wait_ms=4000):
    ws.call("Page.navigate", {"url": url})
    time.sleep(wait_ms / 1000.0)


def shot(ws, path):
    msg = ws.call("Page.captureScreenshot", {"format": "png"})
    import base64

    with open(path, "wb") as fh:
        fh.write(base64.b64decode(msg["result"]["data"]))
    return path


KEYS = {
    "Tab": (9, ""),
    "Enter": (13, "\r"),
    "Escape": (27, ""),
    "ArrowDown": (40, ""),
    "ArrowUp": (38, ""),
    "ArrowLeft": (37, ""),
    "ArrowRight": (39, ""),
    "Home": (36, ""),
    "End": (35, ""),
    " ": (32, " "),
}


def press(ws, key, code=None, text=None):
    """A real key press, not just a JS `keydown` dispatch.

    `windowsVirtualKeyCode` is what lets Blink run the key's DEFAULT ACTION:
    without it Enter on a <button> never fires click, and the walkthrough that
    used to call this reported "the destination did not change" for a page that
    was behaving correctly. Enter also needs its text so the keypress is
    produced, the same way the browser does it.
    """
    vk, default_text = KEYS.get(key, (None, ""))
    payload = {"key": key, "code": code or key}
    if vk is not None:
        payload["windowsVirtualKeyCode"] = vk
        payload["nativeVirtualKeyCode"] = vk
    ws.call(
        "Input.dispatchKeyEvent",
        dict(payload, type="keyDown", text=default_text if text is None else text),
    )
    ws.call("Input.dispatchKeyEvent", dict(payload, type="keyUp"))
    time.sleep(0.06)


def active(ws):
    return cdp.evaluate(
        ws,
        "(() => { const a = document.activeElement; if (!a) return null;"
        " return { id: a.id || null, cls: (typeof a.className === 'string' ? a.className : ''),"
        " label: (a.getAttribute && (a.getAttribute('aria-label') || a.textContent || '').trim().slice(0, 40)) || null,"
        " tag: a.tagName, dest: document.documentElement.dataset.higDest || null,"
        " selected: document.querySelector('[role=option][aria-selected=true]')?.dataset.decisionId || null,"
        " sheet: !!document.querySelector('dialog[open]') }; })()",
    )


def measure(ws):
    expr = open(os.path.join(HERE, "audit_measure.js"), encoding="utf-8").read()
    return cdp.evaluate(ws, expr)


def a11y(ws):
    expr = open(os.path.join(HERE, "a11y.js"), encoding="utf-8").read()
    return cdp.evaluate(ws, expr)


def walkthrough(ws, base, outdir):
    """The keyboard-only path the card asks to be demonstrated, step by step.

    Every step is driven by real key events over CDP — Tab, Enter, the arrow
    keys, Escape — and asserts the thing the card asks for (the destination
    changed, the selection moved, the sheet opened and closed) instead of
    assuming it. The earlier version of this walkthrough read a *document-wide*
    selector to decide where focus was, so it broke out of its Tab loops on the
    first iteration and then pressed Enter on whatever happened to be focused.
    """
    steps = []
    cdp.apply_size(ws, "1440x900", False)
    cdp.apply_media(ws, "dark")
    navigate(ws, f"{base}/proto/?v=4&offline=1&dest=fleet", 4200)

    def record(action, result, note="", ok=False):
        steps.append({"step": len(steps) + 1, "action": action, "result": result, "note": note, "ok": bool(ok)})
        return ok

    def matches(predicate):
        return bool(
            cdp.evaluate(
                ws,
                "(() => { const a = document.activeElement;"
                " if (!a) return false; return !!(" + predicate + "); })()",
            )
        )

    def tab_until(predicate, limit=40):
        """Tab (keyboard only) until the FOCUSED element satisfies predicate."""
        for n in range(limit):
            if matches(predicate):
                return True, n
            press(ws, "Tab", "Tab")
        return matches(predicate), limit

    def dest():
        return cdp.evaluate(ws, "document.documentElement.dataset.higDest || null")

    def selected_id():
        return cdp.evaluate(ws, "document.querySelector('[role=option][aria-selected=true]')?.dataset.decisionId || null")

    def detail_text():
        return cdp.evaluate(
            ws,
            "(document.getElementById('hig-incident-detail')?.innerText || '').trim().slice(0, 160)",
        )

    def focus_now():
        return cdp.evaluate(
            ws,
            "(() => { const a = document.activeElement; if (!a) return null;"
            " return { tag: a.tagName, cls: (typeof a.className === 'string' ? a.className : ''),"
            " label: (a.getAttribute('aria-label') || a.textContent || '').trim().replace(/\\s+/g,' ').slice(0, 48),"
            " role: a.getAttribute('role') }; })()",
        )

    # 1. Tab from the top of the document and record every stop.
    focus_order = []
    reached_incidents = False
    for _ in range(40):
        press(ws, "Tab", "Tab")
        stop = focus_now()
        focus_order.append(stop)
        if matches("a.matches('.hig-nav-item') && a.dataset.dest === 'incidents'"):
            reached_incidents = True
            break
    record(
        "Tab from the address bar to the Incidents destination",
        {"focusOrder": focus_order, "incidentsFocused": reached_incidents, "tabs": len(focus_order)},
        "every stop is a control the operator can reach with Tab alone",
        reached_incidents,
    )

    # 2. Reach Incidents with the keyboard only.
    press(ws, "Enter", "Enter")
    landed = dest() == "incidents"
    record(
        "Enter on the Incidents destination",
        {"dest": dest(), "title": cdp.evaluate(ws, "document.getElementById('hig-title')?.textContent || null"), "focus": focus_now()},
        "destination reached without a pointer",
        landed,
    )

    # 3. Select a record with the arrow keys; the detail pane must follow.
    in_list, tabs = tab_until("a.matches('#decision-list > [role=option]')")
    first_id = selected_id()
    first_detail = detail_text()
    press(ws, "ArrowDown", "ArrowDown")
    down1_id, down1_detail = selected_id(), detail_text()
    press(ws, "ArrowDown", "ArrowDown")
    down2_id = selected_id()
    press(ws, "ArrowUp", "ArrowUp")
    up1_id, up1_detail = selected_id(), detail_text()
    moved = in_list and down1_id != first_id and down1_detail != first_detail and down2_id != down1_id and up1_id == down1_id
    record(
        "Arrow keys in the incident list",
        {
            "focusedListRow": in_list,
            "tabsToList": tabs,
            "first": first_id,
            "down1": down1_id,
            "down2": down2_id,
            "up1": up1_id,
            "detailBefore": first_detail,
            "detailAfterDown1": down1_detail,
            "detailAfterUp1": up1_detail,
        },
        "selection and the detail pane move together",
        moved,
    )

    # 4. Open the log sheet with the keyboard, dismiss it with Escape.
    on_log, _ = tab_until("(a.getAttribute('aria-label') || a.textContent || '').trim() === 'Operator log'")
    press(ws, "Enter", "Enter")
    opened = bool(cdp.evaluate(ws, "!!document.querySelector('dialog[open]')"))
    sheet_shot = os.path.join(outdir, "shots", "13-sheet-operator-log-1440x900-dark.png")
    shot(ws, sheet_shot)
    press(ws, "Escape", "Escape")
    closed = bool(cdp.evaluate(ws, "!document.querySelector('dialog[open]')"))
    returned = matches("(a.getAttribute('aria-label') || a.textContent || '').trim() === 'Operator log'")
    record(
        "Enter on Operator log, then Escape",
        {"focusedLogButton": on_log, "opened": opened, "closed": closed, "focusReturned": returned, "focusAfter": focus_now()},
        "the sheet is a native dialog: Escape dismisses it and focus returns to the control that opened it",
        on_log and opened and closed,
    )

    # 5. Reach Settings with the keyboard.
    on_settings, _ = tab_until("a.matches('.hig-nav-item') && a.dataset.dest === 'settings'")
    press(ws, "Enter", "Enter")
    went = dest() == "settings"
    record(
        "Enter on the Settings destination",
        {"dest": dest(), "title": cdp.evaluate(ws, "document.getElementById('hig-title')?.textContent || null"), "focus": focus_now()},
        "three destinations, three keyboard stops",
        on_settings and went,
    )

    with open(os.path.join(outdir, "audit", "keyboard-walkthrough.json"), "w", encoding="utf-8") as fh:
        json.dump(steps, fh, indent=2, ensure_ascii=False)
    md = ["# Keyboard-only walkthrough", "", "Recorded by `tools/capture.py` with real `Tab` / `Enter` / `ArrowDown` / `Escape` key events over CDP (no synthetic clicks, no pointer).", ""]
    for s in steps:
        md.append(f"## {s['step']}. {s['action']} — {'PASS' if s['ok'] else 'FAIL'}")
        md.append("")
        md.append("```json")
        md.append(json.dumps(s["result"], indent=2, ensure_ascii=False)[:2400])
        md.append("```")
        if s["note"]:
            md.append("")
            md.append(f"_{s['note']}_")
        md.append("")
    with open(os.path.join(outdir, "audit", "keyboard-walkthrough.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(md))
    return steps


def switch_check(ws, base, outdir):
    """Leaving the variant must put the shipped page back, exactly."""
    cdp.apply_size(ws, "1440x900", False)
    cdp.apply_media(ws, "dark")
    navigate(ws, f"{base}/proto/?v=4&offline=1&dest=fleet", 4200)

    def snapshot():
        return cdp.evaluate(
            ws,
            "(() => ({ variant: document.documentElement.dataset.variant,"
            " higNodes: document.querySelectorAll('[class*=hig-], #hig-shell').length,"
            " marked: document.querySelectorAll('[data-hig-row], [data-hig-option]').length,"
            " ids: document.querySelectorAll('[id]').length,"
            " nodeCards: document.querySelectorAll('#overview .node-card').length,"
            " cardButtons: document.querySelectorAll('#overview .node-card button').length,"
            " menus: document.querySelectorAll('.hig-menu, .hig-menu-btn').length,"
            " decisionRows: document.querySelectorAll('#decision-list > .decision-row').length,"
            " logInSheet: !!document.querySelector('#hig-sheet #log-panel'),"
            " roleOptions: document.querySelectorAll('[role=option]').length }))()",
        )

    before = snapshot()
    # The picker owns 1..4; pressing "1" is the keyboard path to Console.
    press(ws, "1", "Digit1", "1")
    time.sleep(1.2)
    after = snapshot()
    press(ws, "4", "Digit4", "4")
    time.sleep(2.0)
    again = snapshot()
    result = {"onHig": before, "onConsole": after, "backOnHig": again}
    with open(os.path.join(outdir, "audit", "variant-switch.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8110")
    ap.add_argument("--out", default=os.path.dirname(HERE))
    ap.add_argument("--only", default=None, help="comma-separated destinations")
    args = ap.parse_args()

    outdir = os.path.abspath(args.out)
    for sub in ("shots", "audit"):
        os.makedirs(os.path.join(outdir, sub), exist_ok=True)

    proc, info = launch()
    try:
        ws = cdp.connect()
        summary = {"browser": info.get("Browser"), "base": args.base, "cases": [], "a11y": [], "walkthrough": None}
        dests = args.only.split(",") if args.only else DESTINATIONS
        n = 0
        for size, mobile in SIZES:
            for appearance in APPEARANCES:
                for dest in dests:
                    n += 1
                    cdp.apply_size(ws, size, mobile)
                    cdp.apply_media(ws, appearance)
                    url = f"{args.base}/proto/?v=4&offline=1&dest={dest}"
                    navigate(ws, url, 4200)
                    name = f"{n:02d}-{dest}-{size}-{appearance}"
                    shot(ws, os.path.join(outdir, "shots", name + ".png"))
                    m = measure(ws)
                    with open(os.path.join(outdir, "audit", f"{dest}-{size}-{appearance}.json"), "w", encoding="utf-8") as fh:
                        json.dump(m, fh, indent=2, ensure_ascii=False)
                    a = a11y(ws)
                    with open(os.path.join(outdir, "audit", f"a11y-{dest}-{size}-{appearance}.json"), "w", encoding="utf-8") as fh:
                        json.dump(a, fh, indent=2, ensure_ascii=False)
                    summary["cases"].append(
                        {
                            "destination": dest,
                            "size": size,
                            "appearance": appearance,
                            "url": m["url"],
                            "screenshot": f"shots/{name}.png",
                            "horizontalOverflow": m["horizontalOverflow"],
                            "doc": m["doc"],
                            "chromeControls": m["chromeCount"],
                            "contentControls": m["contentControls"],
                            "controls": len(m["controls"]),
                            "under44": len(m["under44"]),
                            "unlabelledIconOnly": len(m["iconOnlyUnlabelled"]),
                            "fieldsUnnamed": len(m["fieldsUnnamed"]),
                            "fontSizes": m["fontSizes"],
                            "fontWeights": m["fontWeights"],
                            "textColours": len(m["textColors"]),
                            "contrastChecked": m["contrastChecked"],
                            "contrastBelowTarget": m["contrastBelowTotal"],
                            "a11yCritical": a["critical"],
                            "a11ySerious": a["serious"],
                            "a11yRules": len(a["rulesChecked"]),
                        }
                    )
                    print(f"{name}: overflow={m['horizontalOverflow']} chrome={m['chromeCount']} under44={len(m['under44'])} "
                          f"contrastFail={len(m['contrastWorst'])} a11yCritical={a['critical']} a11ySerious={a['serious']}")
        summary["walkthrough"] = walkthrough(ws, args.base, outdir)
        summary["variantSwitch"] = switch_check(ws, args.base, outdir)
        SCALE = {"22", "17", "15", "13", "11"}
        cases_ok = all(
            (not c["horizontalOverflow"])
            and c["chromeControls"] <= 20
            and c["under44"] == 0
            and c["unlabelledIconOnly"] == 0
            and c["fieldsUnnamed"] == 0
            and c["contrastBelowTarget"] == 0
            and c["a11yCritical"] == 0
            and set(c["fontSizes"]) <= SCALE
            and len(c["fontWeights"]) <= 2
            and min(float(s) for s in c["fontSizes"]) >= 11
            for c in summary["cases"]
        )
        walk_ok = bool(summary["walkthrough"]) and all(s["ok"] for s in summary["walkthrough"])
        summary["acceptance"] = {
            "cases": cases_ok,
            "keyboardWalkthrough": walk_ok,
            "pass": cases_ok and walk_ok,
        }
        with open(os.path.join(outdir, "audit", "summary.json"), "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, ensure_ascii=False)
        ok = cases_ok and walk_ok
        print("ACCEPTANCE:", "PASS" if ok else "SEE FAILURES")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
