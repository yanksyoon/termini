# Client-side evidence — live decision view (telemetry → decider → action → result)

Card **t_5ee69408** ("Client dashboard: live decision view"). Run id **2026-10-15-decisionview**.

Everything here was captured from the **published deployment**, not a mock and not a redrawn
diagram: the page in the address bar is Firebase Hosting, the data in the panels is the
standing controller's, and each PNG has a `.txt` sidecar carrying the raw API/stream output
behind the pixels (read back out of the live DOM at capture time).

## What was under test

| Item | Value |
| --- | --- |
| Published dashboard | `https://termini-510609.web.app` (Firebase Hosting) |
| Bundle | `dashboard/` at `origin/main` `5941029c56f83e594f3d2500294ba3d2cea448ca` (PR #163) |
| Deployed `index.html` sha256 | `4a29ff333369e74299f5469d602579931c6147de3820aa1cd7e7ea690c404c5a` |
| Deployed `js/main.js` sha256 | `3d33b36a056c572de95669a44a4ded34de82b9b7bc22f9e4a720416ac2f1248c` |
| Controller | `https://34-93-190-64.sslip.io/v1`, project `01J0DEM0WARM000000000000AB` |
| Decider under test | forwarded managed model — `backend=gemini-proxy`, `model=gemini-2.5-flash`, `egress=external`, `destination=aiplatform.googleapis.com`, `answered_by=forwarded-managed-model` |
| Capture window (UTC) | `2026-10-15T16:19:44Z` … `2026-10-15T16:27:27Z` |
| Capture host / tooling | Xvfb `1280x900x24`; Google Chrome for Testing `145.0.7632.6` (headful, CDP on `:9222`); ffmpeg `n9.0.1` `x11grab`; fixed window 1280x900 |
| Framing | full window including the browser chrome so the URL is visible in the image; no crop, no annotation, no compositing, no hand-made images |

The demo project token is part of the documented published link (README / SUBMISSION) and
appears in the URLs below and in the address bar of every PNG — it is expected. No other
credential is used anywhere in this evidence, and none appears in a sidecar.

## Steps

| # | Step (acceptance) | What it proves | File | sha256 | Captured (UTC) | Exact URL / command | Sidecar |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 01 | Live **model** decision (**a**) | one row per decision, and for a model decision all of: breaching metric `ram_used_bytes` with its value (96.04% of `ram_total_bytes`) and the rule's own threshold text, the `AI MODEL 0.90` badge with confidence, the model's rationale verbatim, the chosen action (`instance.restart`) + target instance, the execution result and its endpoint (`error · 10.10.0.11:8443`), the audit id, and per-row links to the audit record and the firing telemetry sample | `client/01-model-decision.png` | `24df04b7949bbd0294afc6fe1dac63a93a8e11998ab561e317ca6043dd55a2fa` | `2026-10-15T16:26:27Z` | `https://termini-510609.web.app/?apiBase=https://34-93-190-64.sslip.io/v1&projectId=01J0DEM0WARM000000000000AB&token=demo-project-token&synthetic=1&decisionLimit=200` (filter chip `AI Model`, panel scrolled into view: `bash tools/capture-client-decisions.sh`) | `client/01-model-decision.txt` |
| 02 | Live **fallback** decision (**b**) | the same panel showing a decision the model did NOT make: `FALLBACK` badge (no confidence), `fallback reason inference_unavailable`, playbook text instead of a rationale — visibly different styling from 01, with the same action/target/result/audit links | `client/02-fallback-decision.png` | `44fb6879055b82194fd9857671b2f4f2cab5de437d7fdda8135dd566bdf0c5f3` | `2026-10-15T16:26:41Z` | same URL with the `fallback` filter chip | `client/02-fallback-decision.txt` |
| 03 | Log panel renders events on the published URL (**c**) | the "Live AI-operator log" panel shows **750 shown / 750 buffered** on a cold page load of the plain documented URL — the defect this card fixes; before the fix the same cold load read "0 lines — Waiting for operator events…" | `client/03-log-panel-events.png` | `caef4019373edf3faf10c51c5010465c5184c64cb44039e5a20edcfdd725383a` | `2026-10-15T16:27:00Z` | `https://termini-510609.web.app/?apiBase=https://34-93-190-64.sslip.io/v1&projectId=01J0DEM0WARM000000000000AB&token=demo-project-token` (the link README/SUBMISSION hand a viewer; log panel scrolled into view) | `client/03-log-panel-events.txt` |
| 04 | Honest WHO-DECIDED label (**d**) | the decider band rendered from the egress record: `decider: forwarded managed model · gemini-2.5-flash` + `backend=gemini-proxy · model=gemini-2.5-flash · egress=external · destination=aiplatform.googleapis.com · answered by=forwarded-managed-model · endpoint=http://10.10.0.13:8080` and the note that the decision was not computed by local weights — nothing here is hardcoded; the sidecar shows the `externalModelCall` frame it is parsed from | `client/04-decider-label.png` | `11bfe2430ab34d9e891205889b897a89b2f9300be08c2547076b7780e0581421` | `2026-10-15T16:27:21Z` | same plain published URL, decider band scrolled into view | `client/04-decider-label.txt` |
| 05 | Audit detail dialog (supports **a**) | the audit record behind the row in 01, in full: audit id, decision id, health event, node, rule, action, result, `executed at`, `endpoint https://10.10.0.11:8443`, the LXD call, the idempotency key and the refusal detail — plus the same object as raw JSON | `client/05-audit-detail-dialog.png` | `35d52875e95f725b4a5b079b46e9b1e7ddda1b3939ef594d8b8403aed25c2e3c` | `2026-10-15T16:19:45Z` | synthetic URL, `show…` → the first decision row's `audit` link | `client/05-audit-detail-dialog.txt` |
| — | Landing (**e**) | PR through `scripts/land.sh`; commit / PR / merge sha recorded on the card | — | — | — | `docs/evidence/e2e-screenshots/2026-10-15-decisionview/` | — |

`client/verification.txt` is the capture script's own log: per-step timestamps, the shot
sizes, and the DOM facts read back after each capture.

## Re-running the capture

```bash
bash docs/evidence/e2e-screenshots/2026-10-15-decisionview/tools/start-browser.sh \
  "https://termini-510609.web.app/?apiBase=https://34-93-190-64.sslip.io/v1&projectId=01J0DEM0WARM000000000000AB&token=demo-project-token&synthetic=1&decisionLimit=200"
bash docs/evidence/e2e-screenshots/2026-10-15-decisionview/tools/capture-client-decisions.sh
```

The second command drives the already-running headful browser over its own CDP endpoint
(`tools/cdp_eval.py`, stdlib only — the host has no websocket library) and writes the PNGs,
sidecars and `verification.txt` into `client/`. It needs a **live controller**: with the
fleet down, every panel legitimately renders its error state.

## Caveats — read with steps 03 and 04

1. **The fleet was stopped while this run was in flight.** The standing demo fleet was
   terminated for cost (operator decision, end of day) between the capture of step 02
   (`16:26:41Z`, controller healthy) and step 03 (`16:27:00Z`). Step 03 therefore shows the
   status chip `reconnecting · stream interrupted` and the "Controller error (NETWORK)"
   banner: the panel still holds the 750 lines it had already rendered on that cold load —
   which is precisely the behaviour under test — but a reader should not mistake the banner
   for an empty panel. Steps 03 and 04 carry a full provenance note in their sidecars.
2. **The raw stream text in the sidecars of steps 03 and 04 was fetched at `16:21:00Z`**,
   minutes before those screenshots: at `16:26:5x`/`16:27:2x` the direct fetch could not
   connect (`curl: connection timed out after 8s`) because the controller was gone. Every
   frame carries its own `ts` (retained tail `2026-10-11T17:19:08Z` … `2026-10-15T16:19:39Z`,
   707 data frames), so the two moments cannot be confused. The DOM text in the same
   sidecars is from the capture instant.
3. **Decision rows are from the demo (seeder) nodes** and are labelled `(demo)` because the
   synthetic URL was used (`&synthetic=1`, the documented `include=synthetic` flag). The
   plain published URL is used for steps 03 and 04. Real hardware node ids are visible in
   the sidebar in both cases.
4. **Execution results read `error`** for the rows shown: the audit detail explains it
   (`target_missing: refused PUT /1.0/instances/… — no LXD instance '…' on
   https://10.10.0.11:8443`). That is the defect card **t_895bf6d3** exists to fix (the
   model's chosen target must exist); it is recorded here as found, not smoothed over.

## NOT CAPTURED

- **A chart frame showing the injected fault rising and recovering with the decision
  marker** (required work item 4). The `show on chart` behaviour was verified live on the
  fleet by the previous run of this card (it selected the decision's own node, marked
  `decisions: oom-guard-01 → instance.restart` on the memory chart and drew the rule's
  threshold line), but no pixel evidence was kept from that verification, and by the time
  this evidence run reached it the fleet was already down — an empty chart proves nothing.
  To close it: run the two commands above in an approved live window, then add a step that
  clicks `button[title^="plot "]` on a decision row (`.decision-links`) with a 15m window
  selected and captures `06-chart-fault-recovery.png`.
- **A fully green frame for steps 03 and 04** (stream chip reading `live · resumed from the
  oldest retained line`). Same reason and same remedy: the panel rendered the retained tail
  correctly, but the controller went away mid-run, so those frames show the outage chip.
