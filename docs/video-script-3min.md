# Termini AI-Infra — 3-minute demo video script

**Deliverable for:** AI Builder Cup — mandatory "Comprehensive Documentation"
(a public YouTube/Vimeo or Google Drive link, 3 minutes, showcasing the AI in action).

**Target runtime: 3:00 (180 s).** Timings are spoken-length estimates at a calm
~140 wpm; the `[on screen]` column is what is actually visible, so the AI's work is
shown, not described.

---

## Before you record

| Prep | Detail |
|---|---|
| **Resume the demo first** | The deployed demo is **paused by default** — the seeder is stopped and the node VMs are terminated, so nothing bills GPU time when idle. Before recording, start the seeder on the controller VM:<br>`gcloud compute ssh termini-controller --zone asia-south1-a --command "sudo systemctl start termini-seeder"`<br>Until it runs, the dashboard shows offline nodes and an empty operator log. |
| Open | The live dashboard (one tab, full-screen, zoom that makes the log readable):<br>`https://termini-510609.web.app/?apiBase=https://34-93-190-64.sslip.io/v1&projectId=01J0DEM0WARM000000000000AB&token=demo-project-token` |
| Second tab | A terminal, for the API/audit check at 2:22. |
| Timing note | The operator log starts empty and fills as incidents fire (≈1/minute). **Start recording, then wait for the first log line before you begin talking** — or record the wait and cut it. |
| Do **not** | Show the token on screen if you plan to keep the video public after the event. Crop the URL bar or record in a window without it. |
| Fallback | If no incident fires during your take, the **Risk** slide in the deck and the recorded numbers in §4 of `SUBMISSION.md` cover the same claims — say so rather than stalling. |

---

## Beat 1 — The problem · 0:00–0:25

> **[on screen]** Title card / the dashboard idle, node health visible.
> **[VO]** "Industrial operations run on data that cannot leave the building —
> customer records, source code, national-core IP, factory-floor telemetry. For
> regulated manufacturers and national labs, sending that to a SaaS cloud is not a
> pricing decision, it is a legal one. So the AI never arrives. And when a disk
> degrades or memory runs out at 3am, it pages a human."

## Beat 2 — What Termini is · 0:25–0:45

> **[on screen]** Pan across the dashboard: two nodes, live telemetry, the charts.
> **[VO]** "Termini turns bare on-premise hardware into a self-operating AI fleet.
> A cloud control plane brings the hardware up on day-0, and on day-2 a **governed
> AI operator** watches it and repairs it. Manufacturing, Intelligent Operations:
> operational data in, a real action out — and the data never leaves."

## Beat 3 — The fleet is live · 0:45–1:05

> **[on screen]** Hover `demo-node-a` and `demo-node-c`: CPU, RAM, disk, services,
> `seen 1s ago`. Point at a fault badge such as `disk degrading`.
> **[VO]** "Two nodes, streaming telemetry every second — memory, disk write
> latency, GPU, service health. Notice this node reports a degrading disk — not a
> mock-up, a live condition the system has to deal with."

## Beat 4 — **The AI decides** · 1:05–2:00 *(the core of the video — the AI in action)*

> **[on screen]** The **Live AI-operator log**. Wait for the next line to appear,
> then let it sit on screen. Zoom so the rule id, the action and the confidence are legible.
> **[VO]** "Watch the operator log. A threshold rule just fired — memory crossed
> 95%, twice. The rule only wakes us up; the decision is a **managed model**'s —
> Gemini on Vertex AI, C0 telemetry only. And the model cannot act outside our
> allowlist: a decoding-layer grammar makes an off-allowlist command
> **unrepresentable**, then a validator, then a confidence gate. If the model is
> slow, wrong or unavailable, the operator falls back deterministically and records
> the reason."
> **[on screen]** Open the **incident card**, then expand the **thinking log** — the
> ordered reasoning steps scroll, and the card's provenance badge is legible.
> **[VO]** "The reasoning is on screen too. The incident card answers what happened,
> why, on what evidence, and what was done — and the thinking log under it is the
> controller's own ordered steps: the rule firing, the evidence window, the prompt
> hash, the model call and who answered it, every gate, the action, the execution,
> the audit row. The API field is `decision_trace`; its `decided_by` says whether a
> model or the fallback answered."
> **[on screen]** The action lands and the log shows the result.
> **[VO]** "It executed, the workload recovered, and every step is in the audit trail."

## Beat 5 — Governance held · 2:00–2:22

> **[on screen]** Scroll to a rejected batch, or show the governance line in the deck.
> **[VO]** "The model only ever sees hardware telemetry. Application logs, customer
> payloads, prompts and credentials are classified C2 — refused at the boundary,
> fail-closed, the batch rejected whole. The prompt is a versioned template carrying
> only typed telemetry fields. So it is not a slogan — it is a property you can check."

## Beat 6 — Built on Google Cloud · 2:22–2:45

> **[on screen]** Terminal:
> ```bash
> curl -sS -H "Authorization: Bearer demo-project-token" \
>   https://34-93-190-64.sslip.io/v1/projects/01J0DEM0WARM000000000000AB/nodes | head -c 400
> ```
> Or show the deck's "Built on Google AI & Google Cloud" slide.
> **[VO]** "Under the hood: **Gemini on Vertex AI** for the diagnosis, **Gemma on Node C** for the agents, the dashboard on **Firebase
> Hosting**, the controller on **Compute Engine**, weights in **Cloud Storage**.
> Deployed, not mocked."

## Beat 7 — Scale and close · 2:45–3:00

> **[on screen]** The roadmap slide, then the closing title.
> **[VO]** "Beyond the prototype: learned baselines for the static thresholds,
> MicroCeph for HA storage, real bare-metal onboarding. The product is the
> operations and the governance — not the model weights. Your hardware. Your data.
> Our operations."

---

## Timing sheet

**Re-measured 2026-10-16** (task `t_6c5e9925`, which adds the thinking-log moment
to Beat 4). VO seconds are word counts at the stated ~140 wpm read; the
"window" is what the recording plan gives each beat on the timeline.

| Beat | Content | Planned window | VO (measured) |
|---|---|---|---|
| 1 | Problem | 0:25 | 0:26 |
| 2 | What Termini is | 0:20 | 0:20 |
| 3 | Fleet is live | 0:20 | 0:14 |
| 4 | **The AI decides** — incl. the incident card + **thinking log** | 0:55 | 1:09 |
| 5 | Governance | 0:22 | 0:21 |
| 6 | Built on Google Cloud | 0:23 | 0:15 |
| 7 | Scale + close | 0:15 | 0:15 |
| **Total** | | **3:00** | **2:59** |

The VO now totals **2:59** at 140 wpm — **inside the 3:00 requirement**; the
committed script measured **3:09** because it was never re-measured after the
proxy-disclosure rewrite. Beat 4 needs **~1:09** of the timeline (its 0:55 window
was already 8 s short of its own VO), and the difference comes from beats 3, 6
and 7, whose VO runs under their windows. `[on screen]` waits — the log line
appearing, the thinking log expanding — are cut/trimmed in the edit, not spoken.

---

## Claim discipline

Every number and mechanism above is verifiable in the repo — the point of the video
is to show the AI acting, not to assert. If you ad-lib, keep to these:

- ✅ The threshold rule **detects**; the model **decides**. Never say the model
  detects.
- ✅ Say "**model-driven with a mandatory deterministic fallback**", not "fully
  autonomous with no guardrails" — the guardrails are the feature.
- ✅ Say "**C2 never egresses**" (that is the enforced property). Do **not** claim
  "nothing leaves the premises" — the operator's model call does, under governance
  §7.1/§7.2, and it carries C0 telemetry only.
- ✅ On the thinking log, use the shipped names: the API field is **`decision_trace`**
  (an object with `trace_version`, `decided_by` and an ordered `steps[]`), and
  `decided_by` is `model`, `fallback` or `deterministic`. A deterministic row writes
  **no** trace at all — say that rather than showing an empty timeline. The
  controller-generated fallback carries its **`fallback_reason`** (and the gate that
  rejected the model's answer); never read a fallback row as model output.
- ✅ The live 6-minute demo script (`docs/demo-script.md` §Beat 4) walks the same
  moment in the same order; keep the two in step.
- ✅ On the gate, be precise: a **C2-class field is rejected fail-closed (`422`)**,
  while a merely *unknown* field is **stripped** (deny-by-default, §3 — that is what
  keeps forward-compatibility). Both prevent storage; only the first is a rejection.
- ✅ The **agent-facing** model is **Gemma** (Google's open weights) on Node C; the **diagnosis** uses **Gemini** (Vertex AI) with C0 telemetry only — say which one, don't conflate them.
- ✅ A node from the pre-baked image registers in **150 ms** (measured against a 5 s
  budget). Say "150 ms"; do not round it down to "instant".
- ❌ Do not quote a live decision count — it changes every minute.

## Optional: showing day-0 in the video

The registration claim is the strongest day-0 evidence but is **not visible on the
dashboard** (the deployed demo's nodes are seeded, not booted). If you want it on
screen, it costs ~2 minutes and replaces the fleet shot in Beat 3:

1. Note the fleet is running (`GET /v1/projects/…/nodes` → `nodes: 2`).
2. Create a node from the baked image and `journalctl -u termini-bootstrap | tail`:

   ```
   [termini-bootstrap] REGISTERED role=compute register_ms=150 boot_to_registered_ms=22840
   ```

   The `.baked-offline` sentinel appearing **before** the boot timestamp is what
   proves nothing was installed at boot. The procedure is in
   `infra/gcp/README.md` → "Adopting the baked node image".
3. Delete the node afterwards so it stops billing.
