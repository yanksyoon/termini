#!/usr/bin/env python3
"""Generic CDP call helper for the capture session (stdlib only).

Usage:
  cdp_do.py <port> <Method> '<json params>'      # prints the CDP response JSON
  cdp_do.py <port> shot <out.png> [clip-json]    # full-viewport PNG screenshot
  cdp_do.py <port> viewport <w> <h> <dsf>        # Emulation.setDeviceMetricsOverride
  cdp_do.py <port> clear-viewport                # Emulation.clearDeviceMetricsOverride

Reuses the websocket client in cdp_eval.py so there is one CDP implementation.
"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp_eval import WS, pick_page  # noqa: E402


def main():
    port = sys.argv[1]
    cmd = sys.argv[2]
    ws = WS(pick_page(port)["webSocketDebuggerUrl"])
    if cmd == "shot":
        out = sys.argv[3]
        params = {"format": "png", "captureBeyondViewport": False}
        if len(sys.argv) > 4:
            params["clip"] = json.loads(sys.argv[4])
        msg = ws.call("Page.captureScreenshot", params, timeout=90)
        data = msg["result"]["data"]
        with open(out, "wb") as f:
            f.write(base64.b64decode(data))
        print("wrote %s %d bytes" % (out, os.path.getsize(out)))
    elif cmd == "viewport":
        w, h, dsf = int(sys.argv[3]), int(sys.argv[4]), float(sys.argv[5])
        msg = ws.call("Emulation.setDeviceMetricsOverride",
                      {"width": w, "height": h, "deviceScaleFactor": dsf, "mobile": False})
        print(json.dumps(msg)[:300])
    elif cmd == "clear-viewport":
        print(json.dumps(ws.call("Emulation.clearDeviceMetricsOverride"))[:300])
    else:
        params = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        print(json.dumps(ws.call(cmd, params))[:2000])


if __name__ == "__main__":
    main()
