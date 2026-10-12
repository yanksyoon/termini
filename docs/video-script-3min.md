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
> **[VO]** "Industrial operations run on data that cannot leave the building.
> Customer records, source code, national-core IP, factory-floor telemetry — for
> regulated manufacturers and national labs, sending that to a SaaS cloud is not a
> pricing decision, it is a legal one. So the AI never arrives. And when a disk
> degrades or memory runs out at 3am, it pages a human, because there is no
> operations team on-premise."

## Beat 2 — What Termini is · 0:25–0:45

> **[on screen]** Pan across the dashboard: two nodes, live telemetry, the charts.
> **[VO]** "Termini turns bare on-premise hardware into a self-operating AI fleet.
> A cloud control plane brings the hardware up on day-0, and on day-2 a **governed
> AI operator** watches it and repairs it. This is the Manufacturing theme —
> Intelligent Operations: operational data in, a real action out. And the data never
> leaves."

## Beat 3 — The fleet is live · 0:45–1:05

> **[on screen]** Hover `demo-node-a` and `demo-node-c`: CPU, RAM, disk, services,
> `seen 1s ago`. Point at a fault badge such as `disk degrading`.
> **[VO]** "Two nodes, streaming telemetry every second — memory, disk write
> latency, GPU, service health. Notice this node is reporting a degrading disk.
> That is not a mock-up; that is a live condition the system has to deal with."

## Beat 4 — **The AI decides** · 1:05–2:00 *(the core of the video — the AI in action)*

> **[on screen]** The **Live AI-operator log**. Wait for the next line to appear,
> then let it sit on screen. Zoom so the rule id, the action and the confidence are legible.
> **[VO]** "Watch the operator log. A threshold rule just fired — telemetry crossed
> 95% of memory, twice in a row. That rule is only a wake-up signal. The decision is
> made by **Gemma**, Google's open-weights model, running on an NVIDIA L4 on Cloud
> Run. The model reads the telemetry, reasons about the fault, and chooses the
> remediation — restarting the hung workload.
>
> And here is the discipline: the model cannot do anything we did not sanction. A
> grammar at the decoding layer means a command outside our allowlist has no valid
> encoding — it is not filtered afterwards, it is **unrepresentable**. Then a
> guardrail validator, then a confidence gate. If the model is slow, wrong, or
> unavailable, the operator falls back to a deterministic decision and records both
> the fallback and the reason."
> **[on screen]** The action lands and the log shows the result.
> **[VO]** "It executed. The workload recovered. And every step of that is in the audit trail."

## Beat 5 — Governance held · 2:00–2:22

> **[on screen]** Scroll to a rejected batch, or show the governance line in the deck.
> **[VO]** "The model only ever sees hardware telemetry. Application logs, customer
> payloads, prompts and credentials are classified C2 — they are refused at the
> boundary, fail-closed, and the batch is rejected whole. The model's prompt is a
> versioned template with only typed telemetry fields in it. So the claim isn't
> 'your data never leaves' as a slogan — it's a property you can check."

## Beat 6 — Built on Google Cloud · 2:22–2:45

> **[on screen]** Terminal:
> ```bash
> curl -sS -H "Authorization: Bearer demo-project-token" \
>   https://34-93-190-64.sslip.io/v1/projects/01J0DEM0WARM000000000000AB/nodes | head -c 400
> ```
> Or show the deck's "Built on Google AI & Google Cloud" slide.
> **[VO]** "Under the hood: **Gemma on Cloud Run GPU**, the dashboard on **Firebase
> Hosting**, the controller on **Compute Engine**, weights in **Cloud Storage**.
> Deployed, not mocked — the links are in the submission."

## Beat 7 — Scale and close · 2:45–3:00

> **[on screen]** The roadmap slide, then the closing title.
> **[VO]** "Beyond the prototype: swap the static thresholds for learned baselines,
> move storage to MicroCeph for HA, and onboard real bare metal. The product is the
> operations and the governance — not the model weights. Your hardware. Your data.
> Our operations."

---

## Timing sheet

| Beat | Content | Planned | Running |
|---|---|---|---|
| 1 | Problem | 0:25 | 0:25 |
| 2 | What Termini is | 0:20 | 0:45 |
| 3 | Fleet is live | 0:20 | 1:05 |
| 4 | **The AI decides** | 0:55 | 2:00 |
| 5 | Governance | 0:22 | 2:22 |
| 6 | Built on Google Cloud | 0:23 | 2:45 |
| 7 | Scale + close | 0:15 | 3:00 |

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
- ✅ On the gate, be precise: a **C2-class field is rejected fail-closed (`422`)**,
  while a merely *unknown* field is **stripped** (deny-by-default, §3 — that is what
  keeps forward-compatibility). Both prevent storage; only the first is a rejection.
- ✅ Gemma is **Google's open-weights model**; the closed API (Gemini) is not used.
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
