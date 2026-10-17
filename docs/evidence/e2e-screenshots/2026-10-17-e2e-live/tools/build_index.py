#!/usr/bin/env python3
"""Build INDEX-client.md from client/frames.jsonl (one row per captured frame).

Run this after capture-e2e-client.py:  python3 tools/build_index.py
It refuses to write an index whose numbering has gaps.
"""
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CLIENT = os.path.join(ROOT, "client")
FRAMES = os.path.join(CLIENT, "frames.jsonl")
INDEX = os.path.join(ROOT, "INDEX-client.md")

HEAD = """# Client-side E2E evidence — `2026-10-17-e2e-live`

Card **t_270d09b9** ("E2E client-side walkthrough"). This is the client half of the
`2026-10-17-e2e-live` run; the backend half is `backend/` + `INDEX-backend.md` (card
`t_2191eca4`) under the same run id.

Every frame is a **full-window screenshot of the published Firebase dashboard** taken
from a scripted run against the **live** deployment — headful Chromium inside Xvfb,
grabbed with `ffmpeg -f x11grab` so the browser chrome and address bar are in the
image. No crop, no annotation, no compositing, no hand-made pixels. Each PNG has a
`.txt` sidecar carrying the raw API JSON / DOM values behind the pixels, and
`client/frames.jsonl` is the machine-readable version of the table below.

The demo project token is part of the documented published link (README/SUBMISSION)
and appears in the URL of frame 01 by construction; the deployed bundle strips it from
the address bar (see 01). No other credential appears in any image or sidecar.

## Environment this run captured

| Item | Value |
| --- | --- |
| Published dashboard | `https://termini-510609.web.app` (Firebase Hosting) |
| Controller | `https://34-93-190-64.sslip.io/v1`, project `01J0DEM0WARM000000000000AB` |
| Deployed controller commit | `a89f8521254c1f2b1af25f24d91d4bbc12194873` (`/opt/termini/DEPLOYED_COMMIT`) |
| Deployed hosting bundle | `dashboard/` at `2167176a4c6a5bddcf14a922628b826dd286abcc` (`index.html` sha256 `0375e4ef…`) |
| Fleet | `termini-controller` + `termini-node-a` (compute, 2 vCPU) + `termini-node-c` (8 vCPU, inference service host) RUNNING; `termini-node-b` TERMINATED. No compute was created or started by this card. |
| Capture host / tooling | Xvfb `{w}x{h}x24`, Chromium `145.0.7632.6` headful (CDP `:{port}`), `ffmpeg {ff} x11grab` |
| Framing | fixed `{w}x{h}` window, browser chrome + address bar included, no crop |
"""

NOT_CAPTURED = """
## NOT CAPTURED — the holes, named

{items}
"""


def load():
    rows = {}
    with open(FRAMES) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            rows[r["order"]] = r          # last capture of a step wins (re-runs)
    return rows


def main():
    rows = load()
    orders = sorted(rows)
    want = [f"{i:02d}" for i in range(1, len(orders) + 1)]
    if orders != want:
        sys.exit(f"refusing to write: numbering has gaps, got {orders}")
    head = HEAD.format(w="1280", h="1000", port="9222", ff="9.0.1")
    out = [head, "## Frames\n",
           "| # | card step | what it proves | file | sha256 | captured (UTC) | bytes | URL / command | sidecar |",
           "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for o in orders:
        r = rows[o]
        out.append("| {order} | {card} | {proves} | `{file}` | `{sha}` | {ts} | {b} | `{url}` | `{side}` |".format(
            order=r["order"], card=r["card_step"], proves=r["proves"].replace("|", "/"),
            file=r["file"], sha=r["sha256"], ts=r["captured_at"], b=r["bytes"],
            url=(r.get("url") or "the dashboard URL in the address bar"),
            side=r["sidecar"]))
    out.append("\n## Re-running the capture\n")
    out.append("""```bash
# 1. a fixed-size headful browser on a virtual display (browser chrome in every frame)
bash tools/start-browser.sh "https://termini-510609.web.app/?apiBase=https://34-93-190-64.sslip.io/v1&projectId=01J0DEM0WARM000000000000AB"

# 2. the scripted walkthrough (each step appends its frame + sidecar + manifest row)
python3 tools/capture-e2e-client.py --list
python3 tools/capture-e2e-client.py 01 02 03 04 05 06 07 08
python3 tools/capture-e2e-client.py 09 10 11 12 13 14

# 3. regenerate this index from the manifest
python3 tools/build_index.py
```

The script needs a **live** controller and a signed-in browser; with the fleet down every
panel legitimately renders its error state. It never creates or starts compute: the only
live mutations are the plan it applies (and releases again), one injected demo fault, and —
for frame 14 only — the node-c inference front door pointed at a non-existent model and
restored immediately afterwards.
""")
    with open(INDEX, "w") as fh:
        fh.write("\n".join(out) + "\n")
    print(f"wrote {INDEX} with {len(orders)} rows")


if __name__ == "__main__":
    main()
