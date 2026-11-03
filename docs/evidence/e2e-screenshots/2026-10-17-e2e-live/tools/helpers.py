#!/usr/bin/env python3
"""Thin helpers over the CDP client: evaluate JS, click by id, screenshot, log."""
import hashlib
import json
import os
import subprocess
import time

from cdp_eval import WS, pick_page

PORT = int(os.environ.get("CDP_PORT", "9222"))
FFMPEG = "/home/ubuntu/.hermes/tools/ffmpeg-9.0.1-linux-x64/bin/ffmpeg"
DISPLAY = os.environ.get("SHOT_DISPLAY", ":99")
SCREEN_W = os.environ.get("SHOT_W", "1280")
SCREEN_H = os.environ.get("SHOT_H", "1000")


def connect(port=PORT):
    return WS(pick_page(port)["webSocketDebuggerUrl"])


def js(ws, expr, timeout=180):
    r = ws.call("Runtime.evaluate",
                {"expression": expr, "returnByValue": True, "awaitPromise": True},
                timeout=timeout)
    res = r.get("result", {})
    if res.get("exceptionDetails"):
        return {"exception": res["exceptionDetails"].get("text")}
    return res.get("result", {}).get("value")


def click(ws, sel_or_id):
    """Click an element by CSS selector or bare id, after scrolling it into view."""
    sel = sel_or_id if sel_or_id[0] in "#.[" else f"#{sel_or_id}"
    expr = f"""(() => {{
        const e = document.querySelector({json.dumps(sel)});
        if (!e) return "missing:{sel}";
        e.scrollIntoView({{block: "center"}});
        e.click();
        return "clicked";
    }})()"""
    return js(ws, expr)


def set_input(ws, sel_or_id, value):
    sel = sel_or_id if sel_or_id[0] in "#.[" else f"#{sel_or_id}"
    expr = f"""(() => {{
        const e = document.querySelector({json.dumps(sel)});
        if (!e) return "missing:{sel}";
        e.focus();
        e.value = {json.dumps(value)};
        e.dispatchEvent(new Event("input", {{bubbles: true}}));
        e.dispatchEvent(new Event("change", {{bubbles: true}}));
        return e.value;
    }})()"""
    return js(ws, expr)


def text(ws, sel_or_id):
    sel = sel_or_id if sel_or_id[0] in "#.[" else f"#{sel_or_id}"
    return js(ws, f"""(() => {{
        const e = document.querySelector({json.dumps(sel)});
        return e ? e.textContent.replace(/\\s+/g, " ").trim() : null;
    }})()""")


def scroll_to(ws, sel_or_id, block="center"):
    sel = sel_or_id if sel_or_id[0] in "#.[" else f"#{sel_or_id}"
    return js(ws, f"""(() => {{
        const e = document.querySelector({json.dumps(sel)});
        if (!e) return "missing";
        e.scrollIntoView({{block: {json.dumps(block)}, behavior: "instant"}});
        return "scrolled";
    }})()""")


def scroll_page(ws, y):
    return js(ws, f"(() => {{ window.scrollTo(0, {y}); return window.scrollY; }})()")


def shot(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "x11grab", "-video_size", f"{SCREEN_W}x{SCREEN_H}",
                    "-i", f"{DISPLAY}.0", "-frames:v", "1", path], check=True)
    return path


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def wait_for(ws, expr, timeout=120, interval=1.5, label=""):
    """Poll a JS boolean expression until true. Returns (ok, last_value)."""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        last = js(ws, expr)
        if last is True or (isinstance(last, dict) and last.get("ok")):
            return True, last
        time.sleep(interval)
    return False, last
