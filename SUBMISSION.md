# Termini AI-Infra — AI Builder Cup submission pack

This file maps the competition's **mandatory** submission requirements to the
artifacts in this repo, and states the category. It is the entry point: a judge
should be able to read this page and reach every required deliverable.

---

## 1. Category specification (required)

> **Problem statement: Manufacturing — Intelligent Operations & Industrial Efficiency**

**Why this theme, in the competition's own words.** The theme asks for solutions
that *"demonstrate how AI can transform operational data into meaningful insights,
recommendations, or actions"* across industrial operations, addressing *"efficiency,
quality, reliability"*. Termini does exactly that and nothing else: it takes live
machine telemetry, has a model reason over it, and turns it into a **remediated
action on the infrastructure** — not a dashboard, not a recommendation someone
still has to act on.

The industrial-operations reading is deliberate. Termini's operator is a **day-2
reliability engineer for an on-premise fleet**: it watches memory, disk, GPU and
inference health, and when a resource crosses a threshold it diagnoses the fault
and executes the fix. Every remedy is written to an audit record. The same loop
applies to a factory-floor cluster, an edge inference node, or a lab appliance —
anywhere operational data must stay on the premises.

### Who benefits

Operators who **cannot send their operational data to a SaaS cloud**: regulated
manufacturers, national-infrastructure labs, and small industrial teams with no SRE
bench. Today those teams either ship telemetry off-site (losing the governance
argument) or run unmonitored and let failures page a human at 3am. Termini gives
them day-0 bring-up and day-2 self-healing **without the data leaving**, and the
governance boundary is an enforceable, checkable property rather than a claim.

---

## 2. Mandatory requirements → artifacts

| # | Requirement (competition wording) | Artifact | Status |
|---|---|---|---|
| 1 | **Clear proposal** — "a deck/PPT converted into a PDF", stating problem, benefit, feasibility and scalability | [`deck/termini-demo-deck.pdf`](deck/termini-demo-deck.pdf) (15 slides, 16:9) — source [`deck/index.html`](deck/index.html) | ✅ ready |
| 2 | **Functional prototype** — "working prototype using Google Cloud's AI tools & deployed on Cloud Run/GCP/Firebase" | Live on GCP — links in §3, evidence in §4 | ✅ deployed |
| 3 | **Comprehensive documentation** — "a link to a public YouTube/Vimeo video or public Google Drive link (3 minutes) showcasing your AI creation in action" | [`docs/video-script-3min.md`](docs/video-script-3min.md) — shot-by-shot script | ⚠️ **script ready; the recorded video + public link is a human step — see §5** |
| 4 | **Category specification** | §1 above | ✅ stated |

> The competition page's example list under category specification reads
> "(Healthcare, Education, Sustainability, Accessibility, Social Good)", which does
> not match the six challenge themes listed on the same page. We have followed the
> **challenge themes** list and named the theme verbatim:
> *Manufacturing — Intelligent Operations & Industrial Efficiency*.

---

## 3. Try it — live links

Everything below is deployed and serving now. No local install is required.

### Dashboard (start here — no account needed)

**https://termini-510609.web.app/?apiBase=https://34-93-190-64.sslip.io/v1&projectId=01J0DEM0WARM000000000000AB&token=demo-project-token**

The query parameters are required (the app reads `apiBase`, `projectId`, `token`).
What to look for:

- **Node health** — two nodes streaming live telemetry, refreshed every second.
- **Live AI-operator log** — the model's decisions as they happen. It starts empty
  and fills as incidents fire (roughly one a minute), so give it a minute.
- **Resource utilization** — CPU / RAM / disk write-latency charts.

### API (for the technically curious)

```bash
# Fleet state
curl -sS -H "Authorization: Bearer demo-project-token" \
  https://34-93-190-64.sslip.io/v1/projects/01J0DEM0WARM000000000000AB/nodes

# The AI-operator event stream (SSE — note the token is a query param here,
# because EventSource cannot set headers)
curl -N "https://34-93-190-64.sslip.io/v1/projects/01J0DEM0WARM000000000000AB/logs?token=demo-project-token"
```

### The model endpoint

Gemma runs on Cloud Run with `--no-allow-unauthenticated` — deliberately, so an
LLM is never exposed publicly for demo convenience. Reach it with an identity token:

```bash
gcloud run services proxy termini-gemma --region asia-southeast1 --project termini-510609
```

---

## 4. Google AI + Google Cloud evidence (hard requirement)

| Requirement | What we used | Where |
|---|---|---|
| **Google AI model** | **Gemma 3 4B** (`unsloth/gemma-3-4b-it`, open weights) served by **vLLM** | Cloud Run GPU, `1× NVIDIA L4`, region `asia-southeast1` |
| **Deployed on Cloud Run** | the model service `termini-gemma` | `https://termini-gemma-tylgzp65ma-as.a.run.app` |
| **Deployed on Firebase** | the operator dashboard | Firebase Hosting — `https://termini-510609.web.app` |
| **Deployed on GCP** | controller on Compute Engine, weights on Cloud Storage | `termini-controller` (`n2-standard-2`, `asia-south1-a`); `gs://termini-510609-models/gemma-3-4b-it/` |

Full deployment detail, including the governing constraints and the two deliberate
departures from an all-on-premise shape, is in
[`infra/cloudrun/README.md`](infra/cloudrun/README.md) and the README's
"Deployed stack" section. Reproducible scripts:
[`infra/cloudrun/`](infra/cloudrun/).

### Where exactly the model is in the loop

The threshold rule is the **detector**, the model is the **decider**. A rule firing
is a wake-up signal, never a decision:

```
telemetry (C0/C1)  →  threshold rule fires  →  GEMMA decides the action  →  guardrails  →  LXD executes  →  audit
```

The model's choice is constrained three ways — a **GBNF grammar** under which an
off-allowlist command has no valid encoding, a **param/guardrail validator**, and a
**confidence gate** — with a **mandatory deterministic fallback** that records both
the fallback and its reason. Which decider acted is recorded per-decision in
`decision_source`, so "the AI did it" is auditable rather than asserted.

Governance holds throughout: the model is an **external call permitted only for the
operator loop over C0 telemetry**, under a static versioned prompt template —
`docs/contracts/governance.md` §7.1/§7.2. **C2 (application logs, payloads, prompts,
credentials) never egresses.**

---

## 5. What still needs a human before you submit

I can build and verify everything in this repo, but two items are inherently
outside it. Both are one action each:

1. **Record and upload the 3-minute video.** The script is written and
   timing-checked — [`docs/video-script-3min.md`](docs/video-script-3min.md). Record
   it against the live dashboard, upload to YouTube (public or unlisted-public) or
   Google Drive with link-sharing on, then paste the URL into §2 row 3 **and** into
   the submission form.
2. **Fill the submission form** with the §3 dashboard link and the video link.

I have **not** fabricated a video link, because a placeholder that 404s is worse
than an honest gap for a judge.

---

## 6. Judging criteria → where we address them

| Criterion | Where |
|---|---|
| Meaningful use of Gen AI | §4 — the model *decides* the remediation; it is not a chatbot bolted onto a dashboard. Slide 12 of the deck. |
| Technical implementation | Live GCP deployment (§3); repo test suite; `docs/architecture.md`; acceptance commands in the README. |
| Scalability & sustainability | Deck slide 13 (roadmap); Fleet HA/storage path; model-agnostic serving — the product is ops + governance, not the weights. |
| Problem alignment | §1 — operational data → **actions**, not just insights. |
| Potential impact | §1 "Who benefits". |
| Originality & differentiation | Deck slide 5 — same delegation as SaaS observability, opposite data flow. |
| Innovation in Gen AI use | Grammar-constrained decoding as the safety mechanism: the model *can't* emit an out-of-policy action, rather than being filtered after the fact. |
| Usability & accessibility | Browser-first dashboard, no install; SSH access via short-lived certificates, no passwords to manage. |
| Experience & integration | One loop spanning bootstrap → telemetry → decision → execution → audit, on one screen. |

---

## 7. Repo map (where the substance lives)

| Path | What |
|---|---|
| [`README.md`](README.md) | Product + deployed-stack overview, quickstart, verification |
| [`deck/`](deck/README.md) | Proposal deck (HTML source + PDF export) |
| [`docs/architecture.md`](docs/architecture.md) | Frozen architecture, contracts index, R1–R24 answers |
| [`docs/contracts/governance.md`](docs/contracts/governance.md) | The C0/C1/C2 boundary and the external-model rule |
| [`docs/contracts/operator-decision.md`](docs/contracts/operator-decision.md) | The decision step's contract |
| [`docs/video-script-3min.md`](docs/video-script-3min.md) | 3-minute video script (required deliverable) |
| [`infra/cloudrun/`](infra/cloudrun/README.md) | Reproducible GCP deployment |
| [`controller/`](controller/README.md), [`termini/decision/`](termini/decision) | Controller, operator loop, model adapter |
| [`chaos/`](chaos/README.md) | Fault-injection harness |
