# Metric-expansion dashboard evidence — the new groups as the client sees them

Card **t_8b60acb2** ("Metric expansion: surface the new metrics on the dashboard (units, chart groups,
derived-vs-raw labels, absent-not-zero)"). Profile **frontend**. Parents: the collector card
**t_92aac3c8** (PR #190 → `1592fff`), which recorded the real sample this panel is fed with.
Contracts read, not modified: `docs/contracts/node-telemetry.md` §4.12–§4.15,
`docs/contracts/dashboard.md` §1.2/§1.4, and the research card's ranking
(`docs/research/infra-metric-inventory.md` §2–§3).

## What is real here — and the two things that are not

| Layer | Real? |
| --- | --- |
| `python3 -m app.main` controller | **real** — own temp DB, its own migrations |
| Node registration + telemetry ingest | **real** — single-use bootstrap token, `POST /v1/nodes/register`, `POST /v1/nodes/{id}/telemetry` with the node's own credential |
| **The sample itself** | **real, and not synthesised here.** `sample/node-payload.json` is a copy of `docs/evidence/2026-10-16-metric-expansion/node-payload.json` (`sha256 bdd536ffa99d20fe9f0b35c3d930fa1b61f41492d1cb923488cfe69b82c3dd61`, byte-identical), a real `build_sample()` from the build host, re-runnable with that card's `tools/collect_node_payload.py`. It went through the real governance gate (C2 scan, C1 transform check, redactor version), the real schema validation and the real store — HTTP 202, `accepted: 1, rejected: 0` |
| Dashboard bundle + browser | **real** — the committed `dashboard/` bundle served by `python3 -m http.server` on a second origin, rendered by a real Chromium in Xvfb, driven over CDP |
| Panel text in `capture/*-panel.txt` | **real DOM output** — `innerText` of the rendered cards, read out of the page (`tools/capture_panel.py`), never retyped |
| **The second sample** (`02-absent-not-zero`) | **DERIVED, and labelled so.** `tools/post_real_payload.py --stage absent` removes every §4.12–§4.14 field from the real sample and writes the result to `capture/sample-no-expansion-fields.json`. That is the shape the collector emits when its sources are unreadable (its omit-not-fabricate rule) — it is *not* a recorded node capture, and the ingest summary shows the controller stored it with zero expansion fields |
| **`*-panel.offline-view.txt`** | **DERIVED from the same view model**, rendered by `tools/render-panel.mjs` under node with no browser. It is the pure view model (`buildSampleView`), useful as a browser-free reproduction; the DOM text is the `*-panel.txt` beside it |

No credential is in any URL, PNG address bar, or JSON here. The project credential is a run-local
fixture: it lives in the work directory (0600) and, in the browser, in that tab's `sessionStorage` —
the app's own rule (`dashboard/js/main.js`, `TOKEN_KEY`). The dashboard URL carries `apiBase` +
`projectId` only, and the harness refuses to start if it would carry a `token=`.

## The two states, from one scripted pass

`tools/run-capture.sh` (one controller, one node, one browser) runs:

| # | Sample | Cards | Where |
| --- | --- | --- | --- |
| 1 | the **real** collector payload (`seq 1`) | six sample cards, every value reported: `oom_kill_count 37`, `pgmajfault_count 2,670,694`, `mem_available_bytes 2.25 GiB`, `io_ticks_ms 62,361,848` (cumulative), `io_in_flight 0` (a *measured* zero), inode ratio `8.7 %` (derived), `tcp_retransmit_segs 297,295`, `conntrack 457 / 262,144`, 7 instances | `capture/01-real-sample-*` |
| 2 | the same node, **every §4.12–§4.14 source unreadable** (fields dropped, `seq 2`) | the same six cards, each reading **"not reported — no §4.12–§4.14 fields on this node's latest sample (absent, not zero)"** — never a zero, never an empty card | `capture/02-absent-not-zero-*` |

The left-hand flat charts are empty in state 1 (the stored sample's `ts` is the collector's capture
time, outside the dashboard's query window) — that is honest and irrelevant here: the point of the
capture is the six §4.12–§4.14 cards, which are per-sample facts, not time series.

## Files

| File | What it is / proves |
| --- | --- |
| `sample/node-payload.json` | the real sample, copied byte-for-byte from the collector card (sha256 recorded above) |
| `capture/01-real-sample-node-recent.json` | what the node sees it pushed (`GET /v1/nodes/{id}/telemetry/recent`, node credential) |
| `capture/01-real-sample-node-detail.json` | what the **dashboard** reads: `GET /v1/projects/{pid}/nodes/{id}` → `metrics_latest` with every §4.12–§4.14 field present (`expansion_fields` in the ingest summary lists them) |
| `capture/01-real-sample-ingest-response.json` / `…-ingest-summary.json` | the controller's ingest answer (`accepted 1 / rejected 0`) and the derived-expansion-field inventory |
| `capture/01-real-sample-panel.txt` | the panel's DOM text, fed by that sample |
| `capture/01-real-sample-panel.json` | the same, plus the page URL, card count and wait time |
| `capture/01-real-sample-charts.png` | the `#charts` element as the browser rendered it (all six cards, beyond the viewport) |
| `capture/01-real-sample-viewport.png`, `…-window.png` | the viewport, and the whole Xvfb frame — the address bar is in the second one, so the reader can see WHICH url produced the pixels |
| `capture/01-real-sample-panel.offline-view.txt`, `…-panel.view-model.json` | the same panel from the pure view model under node (no browser) |
| `capture/02-absent-not-zero-*` | the absent-not-zero state: ingest summary (zero expansion fields), node detail, DOM text, view model, PNGs |
| `capture/sample-no-expansion-fields.json` | the derived counter-sample, as posted |
| `tools/run-capture.sh` | the whole pass: controller → register → post the real sample → dashboard + Chromium → capture → post the derived sample → capture |
| `tools/post_real_payload.py` | registers the node, wraps the payload in the frozen batch envelope, posts it, waits for it to be stored, and reads the node detail back as the dashboard does |
| `tools/capture_panel.py` | seeds the tab's config, reloads, waits for the six cards, extracts the DOM text, screenshots, and ASSERTS on the text (a blank panel fails the run) |
| `tools/render-panel.mjs` | the browser-free reproduction of the same view model |
| `tools/cdp.py`, `tools/start-browser.sh` | CDP-over-socket client and the Xvfb+Chromium launcher; copied from `docs/evidence/e2e-screenshots/2026-10-15-decisionview/tools/` so this card does not depend on another card's directory |

## Reproduce

```bash
# the browser pass (controller + browser; no GCP, no fleet, ~40 s)
bash docs/evidence/2026-10-17-metric-expansion-dashboard/tools/run-capture.sh \
     docs/evidence/2026-10-17-metric-expansion-dashboard
# the browser-free panel text for the committed sample
node docs/evidence/2026-10-17-metric-expansion-dashboard/tools/render-panel.mjs \
     docs/evidence/2026-10-17-metric-expansion-dashboard/sample/node-payload.json \
     --out-dir docs/evidence/2026-10-17-metric-expansion-dashboard/capture --prefix 01-real-sample
```

## What this evidence does NOT cover (stated, not glossed)

* **No live node, no GCE.** None is needed: the sample is the collector card's real capture, and the
  panel's job is to render it. Nothing here claims a live run.
* **No threshold was invented.** The contract gives **no** threshold for any §4.12–§4.14 field, so per
  the card's rule 4 **nothing was wired into `degraded_reasons`** — an invented client-side threshold
  would disagree with the controller's own rules. The panel instead names the frozen rule each group is
  a leading indicator for. Flagged on the diagnosis card (`t_922b3268`) instead: if the diagnosis wants
  a `pressure`-based flag, the contract owes it a threshold.
* **The live demo and the offline backend still show "not reported".** `console/seed_demo.py` (tracked
  as `t_a1b5e136`) and `dashboard/mock/fixtures.py` (pinned to the frozen §1.4 19 by
  `tests/test_dashboard_contract.py`) do not carry the new groups; feeding either one fake pressure
  values would be inventing telemetry, so neither was touched. The committed real sample is the input.
* **Entry-level absence is pinned by unit test, not by the DOM capture.** The capture exercises the
  *group-level* absent state; `dashboard/tests/core.test.mjs` ("a disk with unreadable extension fields
  keeps its OWN entry index", "absent ≠ zero") pins the per-row and per-entry cases.
* **Metrics deliberately NOT charted** (research card §2 rows 10, 12–16): `lxd_instances[].cpu_throttled_usec`,
  `pressure[].avg60` (the contract froze `pressure{}` at the four avg10 scalars), `compact_stall/fail`,
  `fd_count/fd_max`, the GCE preemption/maintenance booleans, SMART media health. Rows 1–9 and 11 are
  shipped; this is the ranked set, not every metric.
