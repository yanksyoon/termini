# Client-side E2E evidence — `2026-10-17-e2e-live`

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
| Capture host / tooling | Xvfb `1280x1000x24`, Chromium `145.0.7632.6` headful (CDP `:9222`), `ffmpeg 9.0.1 x11grab` |
| Framing | fixed `1280x1000` window, browser chrome + address bar included, no crop |

## Frames

| # | card step | what it proves | file | sha256 | captured (UTC) | bytes | URL / command | sidecar |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 01 | 01 | the published Firebase link opens the real dashboard and resolves apiBase + projectId from the query string; the legacy ?token= parameter is refused and stripped from the address bar, and the app asks the viewer to sign in instead | `client/01-published-link-open.png` | `34fd0cf5a3f08943761e5db86430ab0401b994e4f9d2c8d15d05c3fd81cfee11` | 2026-10-17T20:24:40Z | 125338 | `https://termini-510609.web.app/?apiBase=https://34-93-190-64.sslip.io/v1&projectId=01J0DEM0WARM000000000000AB&token=demo-project-token` | `client/01-published-link-open.txt` |
| 02 | 02 | after signing in through Settings the node-health panel lists the real registered fleet — node-a (compute, 2 vCPU) and node-c (8 vCPU, the inference host) — both online and reporting real hardware with no GPU, so these are not the seeder's synthetic rows | `client/02-node-health-real-fleet.png` | `3644bade222f6b06dab264be19f6139a68e5add2053e29bf10a639c3b9d26223` | 2026-10-17T20:26:32Z | 102101 | `the dashboard URL in the address bar` | `client/02-node-health-real-fleet.txt` |
| 03 | 03 | the resource-utilization charts are streaming live telemetry (CPU %, memory, disk write latency) for the selected real node, drawn from the controller's /metrics series rather than a static image | `client/03-telemetry-charts-live.png` | `2a72898feacf8f45136d976365033eaaa3d0231f664fcafc0ea8a9d2494a5452` | 2026-10-17T20:32:53Z | 167453 | `the dashboard URL in the address bar` | `client/03-telemetry-charts-live.txt` |
| 04 | 04 | the headline prompt typed into the Scale-intent box exactly as a judge would type it: "deploy 3 worker agents for coding plus a fast LLM" | `client/04-headline-prompt-typed.png` | `9927c42048e43daf9230d1caa1143c2e66956a3eb9bd41c0d024903249d37ea0` | 2026-10-17T20:35:04Z | 123690 | `the dashboard URL in the address bar` | `client/04-headline-prompt-typed.txt` |
| 05 | 05 | POST /projects/{pid}/intents answered 200 with outcome=accepted for the headline prompt: each allocation resolved onto a real node with the guardrail's clamp/reject reasons visible and no rejection | `client/05-plan-accepted.png` | `ce75179ee47a832d1a40f7193c31a2dc2492805f4a6b4d17f57cf6fb2125d2ad` | 2026-10-17T20:35:26Z | 140009 | `the dashboard URL in the address bar` | `client/05-plan-accepted.txt` |
| 06 | 06 | POST /projects/{pid}/vms/apply returned state=ready with the plan's real vm ids, and the dashboard's Agents panel lists those instances as running on node-a, so the plan became real LXD instances rather than a drawing | `client/06-instances-running.png` | `1a000e75d27fb5ea5e2832084a2b4259faf47d704d331bb977a42a68f80afb74` | 2026-10-17T20:41:17Z | 180943 | `the dashboard URL in the address bar` | `client/06-instances-running.txt` |
| 07 | 07 | the live AI-operator log stream carries the decision's own provenance: the externalModelCall lines name endpoint/backend/egress/model/destination=aiplatform.googleapis.com and answered_by=forwarded-managed-model, i.e. the decision was answered by the forwarded managed model through node C's inference front door and not by local weights | `client/07-decider-forwarded-managed-model.png` | `34488b5cda962995f5f3c7cf5bb912430880a93b744571a6423100b390a3acb6` | 2026-10-17T20:51:32Z | 262951 | `the dashboard URL in the address bar` | `client/07-decider-forwarded-managed-model.txt` |
| 08 | 07 | the only node with a GPU is the seeder's simulated one and the dashboard shows it labelled as such (demo rows are opt-in through the 'demo nodes' toggle); every real node reports gpus: [] and role=compute | `client/08-simulated-gpu-node-labelled.png` | `f3a40e3144774ad459288b7447d8beb6e9a2e1fba4da1ff0f738ddc4d013b7a6` | 2026-10-17T20:52:22Z | 197312 | `the dashboard URL in the address bar` | `client/08-simulated-gpu-node-labelled.txt` |
| 09 | 08 | an injected OOM scenario on the REAL node-a produced a guardrail-admitted remediation: the decision names the rule (oom-guard-01), the action (instance.restart) and its target, and the audit row records the LXD call that executed it against 10.10.0.11:8443 | `client/09-oom-remediation-executed.png` | `ef589ca78f05423504f3ce40d841d79741329c2e6c0920850b7dc8be44399e07` | 2026-10-17T20:53:29Z | 206314 | `the dashboard URL in the address bar` | `client/09-oom-remediation-executed.txt` |
| 10 | 09 | the audit row opens from the decision and names what executed: action_kind, the target instance, result=ok, executed_at, the LXD operation and the endpoint that was dialled, with the same row reproduced as raw JSON | `client/10-audit-trail-row.png` | `2dfaafc2fc8c44100ecbe6817673db91e8d6f51a63c8ad30c2b1f5f03c2c103c` | 2026-10-17T20:53:37Z | 206224 | `the dashboard URL in the address bar` | `client/10-audit-trail-row.txt` |
| 11 | 10 | the web-terminal control does NOT exist as a reachable dashboard control (the link stays hidden), while /terminal/ itself serves and the controller's node-SSH endpoint answers — the gap is captured rather than omitted | `client/11-web-terminal-access.png` | `666aa9e5f73cfd4f2e7d2c6a16e66b619269d42696c290e311c4354e96504257` | 2026-10-17T20:53:51Z | 136570 | `the dashboard URL in the address bar` | `client/11-web-terminal-access.txt` |
| 12 | 11 | the RESET releases the plan's committed capacity back to node-a and the audit trail is preserved; the fleet is back at its pre-plan capacity for the next visitor | `client/12-reset-capacity-released.png` | `175c21c13b3206de64a5f97f45d85c3ddd9b8f47cfd3f3a5f8c8686006f6b064` | 2026-10-17T20:54:26Z | 117726 | `the dashboard URL in the address bar` | `client/12-reset-capacity-released.txt` |
| 13 | 11 | the headline prompt "deploy 3 worker agents for coding plus a fast LLM" is accepted again after the reset (200 accepted, no rejection), so the reset left the fleet in a working state rather than merely freeing numbers | `client/13-post-reset-prompt-succeeds.png` | `13394609c108a7a8cbc75f3eb0f173e4262fcfc52a31ad0adbe4fab3ec0a507e` | 2026-10-17T20:58:51Z | 107710 | `the dashboard URL in the address bar` | `client/13-post-reset-prompt-succeeds.txt` |

## Re-running the capture

```bash
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

## NOT CAPTURED — the holes, named

| card step | what is missing | why |
| --- | --- | --- |
| 12 — deliberate failure view | **not captured** | The run reached its wall-clock budget. The plan for it is written and re-runnable (`capture-e2e-client.py 14`: point node-c's `TERMINI_GEMINI_MODEL` at a non-existent model, restart `termini-inference-proxy`, inject one fault, wait for `decision_source=fallback`, then restore and verify `/health`). It was NOT run, so no frame is offered for it and the demo's inference backend was left untouched. |
| 09 — audit row "behind the dialog" | the dialog itself did not open | The script selected an executed decision and clicked the audit link, but this build opened no dialog (`dialogs: []` in the sidecar). The frame shows the decision detail with the `audit 01M55T6…` link; the raw audit rows (action_kind, target, result, executed_at, LXD method/path/endpoint) are in `client/10-audit-trail-row.txt`. |
| 05/06 — the Apply path as a judge sees it | partially | The dashboard's own Apply is bounded by a 20 s client timeout, so the UI reported `NETWORK: controller unreachable: signal timed out` while the controller finished the apply server-side; `client/06-instances-running.txt` carries that error text plus the post-apply Agents panel (6 → 8 running instances on node-a). The plan's own vm ids are in `client/05-plan-accepted.txt` (plan `01M55S73GAJK1S1T03YZTKNK68`, allocation 0 accepted on node-a) and the materialisation is proven on the node side. |
| 10 — a reachable web terminal | the dashboard control does not exist | `#link-terminal` is in the markup but nothing unhides it; the frame shows `/terminal/` itself (200) and the sidecar carries the controller's node-SSH endpoint. |
| 02 — "node-c inference" | re-worded | Both real nodes report `role=compute` (the frozen role rule needs a >=12 GiB GPU and this fleet is CPU-only after the proxy pivot). node-c is the inference **service host**, which is what frame 02's sidecar says. |
