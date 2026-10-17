# Client dashboard evidence — follow-up run `2026-10-17-decisionview-live`

Card: **t_4386b903** (follow-up to t_5ee69408). Owner: `frontend`.
This run dir is new; `docs/evidence/e2e-screenshots/2026-10-15-decisionview/` and the
sre card's `t_270d09b9` tree are untouched.

## What this run owes, and what it produced

| # | The card asks for | Outcome |
| --- | --- | --- |
| 1 | `06-chart-fault-recovery.png` — the injected fault on the chart: metric rising above the rule's threshold and recovering, decision marker + threshold line in one frame | **CAPTURED** (rise, hold at 97.00%, recovery, decision marker, `fault` card styling) — **threshold line NOT IN FRAME**, and the reason is a product defect proven below, not a capture miss |
| 2 | Fresh `03`/`04` frames whose stream chip reads `live · resumed from the oldest retained line` | **NOT CAPTURED** — the published dashboard cannot open the log stream at all (401 on the only auth style it sends). Labelled frames `03-log-panel-events-red.png` / `04-decider-label-red.png` are kept, with the probes, rather than left as a silent hole |

Neither miss is a window problem: the standing fleet was **up** for the whole run
(nodes reporting, decisions flowing, real LXD restarts executed — see each sidecar's
audit row). Both are defects in code that is on `main` today.

## Bundle under test

| Item | Value |
| --- | --- |
| Hosting | `https://termini-510609.web.app` (Firebase Hosting) |
| Controller | `https://34-93-190-64.sslip.io/v1`, project `01J0DEM0WARM000000000000AB` |
| Bundle identity | every file used is **byte-identical** to working tree `origin/main` @ `0d3b248` |
| `index.html` sha256 | `ce199a0970bd50e2c575d6bdfb5849ff136422ad3d86f4ceeaf16f22a8194d4e` (= local) |
| `js/main.js` sha256 | `72492a46719e74f4772cc659a1b446c0a2d724ffd9e2c06d102344148b9bf0dd` (= local) |
| `js/stream.js` sha256 | `6cecad378c7db0c17058e0f26f74c49955da87e20df0bdb4c6835adee7acad11` (= local) |
| `js/core/series.js` sha256 | `83b5c765a702a9a5c25d0d2fe636957b13472f3745625c21dfe416ed8864d724` (= local) |
| `js/core/decisions.js` sha256 | `798d99d89665d173…` (= local) |
| `js/ui/metrics.js` sha256 | `638f8913b1ae7dc2…` (= local) |
| Capture window (UTC) | `2026-10-17T03:39:18Z` … `2026-10-17T04:01:43Z` |
| Capture host / tooling | Xvfb `:99` 1280x1700x24; Chrome for Testing `145.0.7632.6` (headful, CDP `:9222`); ffmpeg `n9.0.1` `x11grab`; window `1280x1700` for 03/04 and `1000x1700` for 06 |
| Framing | the whole browser window including the chrome, so the URL is visible in the image; no crop, no annotation, no compositing, no hand-made pixels. Window size is stated per frame because 06 needs the dashboard's one-column layout (at 1280 px the charts sit in a 466 px rail beside a 2 875 px collapsed log panel, which renders this card at 444x150 in a black void) |

## Frames

| # | What it proves | File | sha256 | Captured (UTC) | URL in the address bar | Sidecar |
| --- | --- | --- | --- | --- | --- | --- |
| 03 | the log panel and its stream chip **as they really render**: panel open, `0 lines — Waiting for operator events…`, chip `reconnecting · stream interrupted (Failed to fetch)`, header pill `stream reconnecting` | `client/03-log-panel-events-red.png` | `efb5fb7c4469788d16ec6d76c8369442e8f5c5ac6cf23b2ff177fc5923171704` | `2026-10-17T03:42:35Z` | `https://termini-510609.web.app/?apiBase=https://34-93-190-64.sslip.io/v1&projectId=01J0DEM0WARM000000000000AB` | `client/03-log-panel-events-red.txt` |
| 04 | the decider band **as it really renders**: `decider: decider unknown` / `no externalModelCall record on the operator log stream yet`, above live decision rows (breach `ram_used_bytes` 96.04%, rule threshold text, executed `ok · 10.10.0.11:8443`) | `client/04-decider-label-red.png` | `ffecfa8f52ee3fdc5fd7f094e0e85f8bbcf21bd093f86bd08852d206675912ac` | `2026-10-17T03:42:37Z` | same plain published URL | `client/04-decider-label-red.txt` |
| 06 | the injected fault on the Memory chart, 15m window, `demo-node-a` selected: baseline 26.19% → peak **97.00%** of `ram_total_bytes` → hold → back to 26.19%, with the `oom-guard-01 → instance.restart` decision marker on the rise and the card's fault styling. Threshold line **absent** — cause is proven in the sidecar | `client/06-chart-fault-recovery.png` | `4a601a81f847cb98fa6a05c410f7cedd361dcfcd56e85a9d3b8d401c9ddf3584` | `2026-10-17T04:01:41Z` (re-capture; first capture `03:58:07Z` kept in `client/superseded/`) | `https://termini-510609.web.app/?apiBase=https://34-93-190-64.sslip.io/v1&projectId=01J0DEM0WARM000000000000AB&synthetic=1&decisionLimit=200` | `client/06-chart-fault-recovery.txt` |

`client/06-metrics-15m-5s.json` is the payload the page itself received for the draw;
`client/06-metrics-15m-5s-recheck.json` is the independent re-fetch of the same window
used for the numbers in the re-capture section.

## NOT CAPTURED — item 2: a stream chip reading `live`

The published dashboard cannot open `GET /v1/projects/{pid}/logs` at all. Two
independent causes, both proven live in `client/03-log-panel-events-red.txt`:

1. **The controller's SSE route authenticates from the QUERY string only.**
   `controller/app/server.py:_handle_sse_logs` -> `self._require_project_token(q.get("token"))`.
   Probed with the demo token:

       GET /v1/projects/01J0DEM0WARM000000000000AB/logs?since=000…0
         with  Authorization: Bearer <token>            -> HTTP 401 UNAUTHENTICATED
         with  Authorization + Cache-Control: no-cache   -> HTTP 401 UNAUTHENTICATED
         with  ?token=<token>                           -> HTTP 200 + text/event-stream

2. **The CORS preflight does not allow a header the client sends.**
   `dashboard/js/stream.js:111` sends `{"Accept": "text/event-stream", "Cache-Control": "no-cache", "X-Termini-Version": "1"}`:
   the controller answers
   `access-control-allow-headers: Authorization, Content-Type, Idempotency-Key, X-Termini-Version, Last-Event-ID`
   — `Cache-Control` is missing, so the browser rejects the request before it ever
   sees a status (`TypeError: Failed to fetch`, which is exactly the chip text).

Cause 1 is a regression from `t_5649d117`, which correctly removed the credential
from the URL and moved the SSE client to the `Authorization` header — but the
controller route was never taught to read it. The knock-on effects are visible in
these very frames: the log panel renders nothing (there is no REST backfill — the
panel is SSE-only), and the decider band in 04 is stuck on `decider unknown` because
it is parsed from `externalModelCall` lines on that same stream. A labelled fallback
or deterministic decision is a correct capture; a stream that never opens is not, so
03/04 are named `…-red` and are **not** offered as the acceptance frames.

## NOT CAPTURED — the threshold line inside 06

`ram_used_bytes` really does rise above the rule's threshold (97.00% vs 95%, 11
buckets, then back to 26.19% — the table is in the sidecar), but the dashed threshold
line cannot be drawn by this bundle: `thresholdMarker()` needs the denominator series
`ram_total_bytes`, and `requestedMetrics()` only asks for chart-group metrics, which
does not include it. Full proof, with the exact `/metrics` query the page issued and
the `thresholdInCaption: false` readback, is in
`client/06-chart-fault-recovery.txt` -> "Why there is no threshold line".

**Update (t_497c24fa, after this pack landed):** the cause is fixed on `main` — the
dashboard now asks `/metrics` for `ram_total_bytes` (plus `disk_total_bytes` and
`gpu_vram_total_bytes`) as a *threshold denominator*, so `thresholdMarker()` resolves
`>= 95% of ram_total_bytes`, `buildCharts()` puts the value in the memory group's
`thresholds`, and `charts.js` draws the dashed line labelled
`oom-guard-01: 95% of ram_total_bytes` with `threshold: …` back in the card caption.
The frames in this pack predate that fix and are unchanged: re-shooting
`06-chart-fault-recovery` against a live window is still outstanding.

## Judgement calls, stated

- The fault is injected through the documented operator endpoint
  (`POST /v1/projects/{pid}/demo/faults`, controller-api.md §4.7) on `demo-node-a`, a
  **seeder (synthetic)** node that the dashboard labels `(demo)`. It is held and then
  cleared, so the same 15 m window holds the rise, the hold and the recovery. No real
  hardware telemetry was altered and no compute was created.
- The AI path was degraded for the whole window (newest rows are `FALLBACK` /
  `DETERMINISTIC`), exactly as the operator's unblock note warned. Every frame states
  what it shows; nothing is relabelled as a model decision.
- The 06 frame is a re-capture from the same page, the same plotted decision and the
  same 15 m window, taken three minutes after the first shot so that an earlier
  episode of this card had left the window and the frame shows one episode instead of
  two. No new injection, no re-plot, no editing — the first capture is kept unedited
  in `client/superseded/`.

## Reproduce

    bash tools/start-browser.sh "https://termini-510609.web.app/?apiBase=https://34-93-190-64.sslip.io/v1&projectId=01J0DEM0WARM000000000000AB&synthetic=1&decisionLimit=200"
    STEPS="03 04" bash tools/capture-client-live.sh      # the two labelled frames + the probes
    STEPS="06" PREWAIT_S=180 HOLD_S=240 SETTLE_S=30 bash tools/capture-client-live.sh
    bash tools/recapture-06.sh 130                        # optional: one-episode re-frame
