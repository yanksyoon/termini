# Termini AI-Infra — AI Builder Cup submission pack

This file maps the competition's **mandatory** submission requirements to the
artifacts in this repo, and states the category. It is the entry point: a judge
should be able to read this page and reach every required deliverable.

---

## 1. Category specification (required)

> **Problem statement: Future of Work & Enterprise Productivity**

**Why this theme, in the competition's own words.** The theme asks for solutions
that *"transform how individuals and organizations access knowledge, collaborate,
make decisions, and complete everyday work"* and show *"meaningful improvements in
productivity, efficiency, knowledge accessibility, or workflow execution."* That is
Termini's customer and Termini's job almost literally: a twelve-person clinic
answers its phones, reconciles insurance claims, finds the answer to "how did we do
this last time?", and produces its own management reports — with AI, on hardware it
owns, and without a single record leaving the building.

**Why not Manufacturing.** v1 was filed under *Manufacturing — Intelligent
Operations & Industrial Efficiency*, which the page scopes to "manufacturing and
industrial operations." Termini's deliverables are industrial-grade — bootstrap,
day-2 remediation, a fail-closed governance boundary — but **the buyer in this
submission is a small organisation, not a factory**. Filing a clinic's operating
model under Manufacturing described our buyer as something they are not. The
operational loop is unchanged: telemetry in, a model reasons over it, an action
comes out, the action is audited.

**The work being transformed.** Termini turns bare, on-premise hardware into a
self-operating AI fleet: a cloud control plane registers and joins the node on
day-0, a governed AI operator watches and remediates it on day-2, and the data
boundary is enforced (C2 never egresses) rather than asserted. The productivity
claim is the one the clinic already proved by hand: **the work that used to be
done by an extra hire, an accountant and a handful of SaaS subscriptions runs on the box
in the back room** — except that nobody has to spend six months of evenings
building it.

### Who benefits

Two sides of the same market:

1. **Small and mid-sized organisations that hold governance-sensitive data** —
   clinics and practices, professional-services firms, workshops, local
   manufacturers, public institutions. They either ship their data to a SaaS
   cloud (losing the governance argument) or run unmonitored and page a human at
   3am. They have no SRE bench, and they are the majority of the economy.
2. **Hardware owners** — colocation, edge and private-cloud operators who already
   own the metal and the power, and cannot sell it as AI because they cannot staff
   the operations. Termini is the layer that makes their hardware sellable.

---

## 2. Mandatory requirements → artifacts

| # | Requirement (competition wording) | Artifact | Status |
|---|---|---|---|
| 1 | **Clear proposal** — "a deck/PPT converted into a PDF", stating problem, benefit, feasibility and scalability | [`deck/v2/termini-deck-v2.2.pdf`](deck/v2/termini-deck-v2.2.pdf) (18 slides, 16:9) — source [`deck/v2/index.html`](deck/v2/index.html). v1 kept at [`deck/termini-demo-deck.pdf`](deck/termini-demo-deck.pdf) | ✅ ready |
| 2 | **Functional prototype** — "working prototype using Google Cloud's AI tools & deployed on Cloud Run/GCP/Firebase" | Live on GCP — links in §3, evidence in §4 | ✅ deployed |
| 3 | **Comprehensive documentation** — "a link to a public YouTube/Vimeo video or public Google Drive link (3 minutes) showcasing your AI creation in action" | [`docs/video-script-3min.md`](docs/video-script-3min.md) — shot-by-shot script | ⚠️ **script ready; the recorded video + public link is a human step — see §5** |
| 4 | **Category specification** | §1 above | ✅ stated |

> The competition page's example list under category specification reads
> "(Healthcare, Education, Sustainability, Accessibility, Social Good)", which does
> not match the six challenge themes listed on the same page. We have followed the
> **challenge themes** list and named the theme verbatim:
> *Future of Work & Enterprise Productivity*. (v1 named *Manufacturing —
> Intelligent Operations & Industrial Efficiency*; see §1 for why v2 changed.)

---

## 3. Try it — live links

Everything below is deployed and serving now. No local install is required.

### Dashboard (start here — no account needed)

**https://termini-510609.web.app/?apiBase=https://34-93-190-64.sslip.io/v1&projectId=01J0DEM0WARM000000000000AB**

> **No credential in the URL, on purpose.** Open the link, then use
> **Settings → sign in** once with the project token: it is sent in the
> `Authorization` header from then on and kept for that browser tab only, so any
> link you copy carries no credential. A legacy `?token=…` link is *refused* —
> the dashboard strips the parameter out of the address bar and asks you to sign
> in instead (task `t_5649d117`).

> **The demo is paused by default**, so it does not bill compute time while
> nobody is looking at it: the seeder is stopped and the node VMs are terminated.
> In that state the dashboard loads but shows **offline nodes and an empty
> operator log**.
>
> **Paused is not the same as free.** Terminating an instance stops the vCPU/RAM
> charge, but everything attached to it keeps billing, and so does the reserved
> IP that *is* this demo's URL. Measured on 2026-10-15 with every instance
> TERMINATED and Cloud Run at zero, the project still billed **$37.77/mo**:
> 210 GiB of disks $25.20 + 60 GiB of node images $3.60 + the static IP
> `34.93.190.64` $8.76 (billed as in-use while associated with the instance,
> running or stopped) + the 10.37 GiB model bucket $0.21. After dropping the two
> superseded images the idle floor is **$35.37/mo**. Letting the fleet sit
> "stopped" is a hosting decision, not a zero-cost one.
>
> **Resume it for a judging session** (~30 s to come back):
>
> ```bash
> gcloud compute ssh termini-controller --zone asia-south1-a \
>   --command "sudo systemctl start termini-seeder"
> ```
>
> The seeder self-provisions its own single-use bootstrap tokens at start-up
> (`POST /v1/bootstrap-tokens`, loopback-only), so no hand-minted token is needed
> and a restart — or a fresh take — just works.
>
> Pause again with `sudo systemctl stop termini-seeder`. The Cloud Run GPU scales
> to zero on its own once traffic stops.
>
> **Pause vs full cleanup.** Two different operations, two different costs:
>
> * **`infra/gcp/teardown.sh --pause`** stops every instance and scales Cloud Run
>   to zero, and **deletes nothing** — disks, images, the model bucket and the
>   reserved IP stay, so the demo is re-raisable in ~30 s with no re-bake. The
>   idle floor above still applies while it is paused.
> * **`infra/gcp/teardown.sh --full`** deletes every billable resource —
>   instances and their disks, the addresses and forwarding rules, the images
>   (family included), the Cloud Run service and its revisions, the Artifact
>   Registry repository, the model bucket with its objects and the
>   non-Google-managed log buckets — and drives the idle spend to **$0**. It
>   refuses to run while the demo retention freeze is active (through 6 Nov 2026)
>   because `termini-controller-ip` is this published URL, and it exits non-zero
>   if anything it meant to delete survives.
> * **`infra/gcp/tools/cleanup-verify.sh`** is the check for both: it prints every
>   billable-class resource with its class, region, size and whether it bills
>   while idle, and exits non-zero while any of them is still there. Raw output
>   of every run: `infra/gcp/evidence/cleanup-2026-10-15.md`.

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

# The AI-operator event stream (SSE). The dashboard reads this with `fetch()`
# and an Authorization header — no credential in the URL (task t_5649d117).
curl -N -H "Authorization: Bearer demo-project-token" \
  https://34-93-190-64.sslip.io/v1/projects/01J0DEM0WARM000000000000AB/logs
```

### The model endpoints

**Agent-facing — Node C.** Client/agent inference is served from `termini-node-c` (CPU-only),
reachable only over the VPC underlay at `http://10.10.0.13:8080`. Since the pivot
(`t_957956e5` / `t_3fcee90b`) that front door ships `backend=gemini-proxy`: the request is
answered by the managed model and every response labels it in-band (`backend`, `answered_by`,
`served_model`), because the live-GPU path is blocked by the L4 quota and that node's own CPU
weights measured 402 s on the operator's prompt. The on-prem configuration is the one-variable
fallback: `backend=local` serves Gemma 3 4B under `llama.cpp` (CPU-only, alias `gemma`) from
that node's own weights and forwards nothing — CPU-slow (≈8–10 tok/s), stated rather than hidden.
Either way it is the same code path as the ADK/agent clients — no public exposure at all.

**Provider-only — the controller (Gemini).** The operator's **diagnosis** runs on the controller
against a loopback listener (`http://127.0.0.1:8085`, `backend=gemini-proxy` →
`aiplatform.googleapis.com`, answered by `gemini-2.5-flash`). The substituted prompt is C0 telemetry
only (rule/metric context plus the instance inventory — no workload payloads) and the user decided on
2026-10-15 that this prompt may leave the premises as part of the managed service. The fleet has no
route to that listener; it exists only for the diagnosis, which is why the demo's model decision is
seconds rather than the minutes the CPU node needs.

**Managed reference — Cloud Run.** The same Gemma 3 4B is also deployed on Cloud Run with
`--no-allow-authenticated` — deliberately, so an LLM is never exposed publicly for demo
convenience. Reach it with an identity token:

```bash
gcloud run services proxy termini-gemma --region asia-southeast1 --project termini-510609
```

---

## 4. Google AI + Google Cloud evidence (hard requirement)

| Requirement | What we used | Where |
|---|---|---|
| **Google AI model** | **`gemini-2.5-flash`** on Vertex AI — the shipped agent-facing backend through Node C's front door (`backend=gemini-proxy`, since the pivot `t_3fcee90b`); the on-prem fallback is **Gemma 3 4B** (`unsloth/gemma-3-4b-it-GGUF`, Google open weights) served by **llama.cpp** (`backend=local`) | **Node C** (`termini-node-c`, GCE `n2-standard-8`, CPU-only), `10.10.0.13:8080` (alias `gemma`); weights from `gs://termini-510609-models/gemma-3-4b-it-gguf/` |
| **Provider diagnosis (Gemini)** | **`gemini-2.5-flash`** on **Vertex AI `aiplatform.googleapis.com`** — the operator's diagnosis call only | the controller's loopback listener `127.0.0.1:8085` (`backend=gemini-proxy`); audit row `egress=external destination=aiplatform.googleapis.com` |
| **Managed reference model** | the same Gemma 3 4B served by **vLLM** | Cloud Run GPU, `1× NVIDIA L4`, `asia-southeast1` — deployed, not the shipped decider |
| **Deployed on Cloud Run** | the reference model service `termini-gemma` | `https://termini-gemma-tylgzp65ma-as.a.run.app` |
| **Deployed on Firebase** | the operator dashboard | Firebase Hosting — `https://termini-510609.web.app` |
| **Deployed on GCP** | controller on Compute Engine, the decider on Compute Engine, weights on Cloud Storage | `termini-controller` (`n2-standard-2`, `asia-south1-a`); `termini-node-c` (`n2-standard-8`, CPU-only); `gs://termini-510609-models/` |
| **Pre-baked node image** | `termini-node-os-1` (family `termini-node-os`) — carries the bootstrap daemon, so a node registers with **no installs at boot** | GCE image in project `termini-510609` |

### Claims we measured, not asserted

| Claim | Measured |
|---|---|
| A node from the pre-baked image registers in under 5 s | **`register_ms=150`** — ~17× margin. Corroborated: the node shows `online role=compute` at the controller, `cloud-init: done`, the daemon binary timestamped at **bake** time, and the `.baked-offline` sentinel present. |
| Plain English → a validated, clamped plan | `200 accepted`; the fast-LLM allocation resolves `realized_as: host_service` (the inference service, not a VM); a 128 vCPU / 512 GB request is **clamped** to 8/16 with auditable reasons. |
| A C2 field is rejected before any byte is stored | Nothing C2 reaches storage, checked three ways: unknown fields stripped, C2 in an allowlisted field → `422`, untransformed C1 → `422`. C2-class field *names* now fail closed (`422`) rather than being silently discarded. |
| Faults are remediated autonomously | Model-decided remediations executing `result=ok`, recorded in the audit trail. |

The independent gate verdict is [`w7/verdict.md`](w7/verdict.md): **GO**, with its accepted residuals listed and one caveat stated inside the document (the re-gate was authored by the same session that fixed several findings, so a separate reviewer should re-run it before treating it as final).

Full deployment detail, including the governing constraints and the two deliberate
departures from an all-on-premise shape, is in
[`infra/cloudrun/README.md`](infra/cloudrun/README.md) and the README's
"Deployed stack" section. Reproducible scripts:
[`infra/cloudrun/`](infra/cloudrun/).

### Where exactly the model is in the loop

The threshold rule is the **detector**, the model is the **decider**. A rule firing
is a wake-up signal, never a decision:

```
telemetry (C0/C1)  →  threshold rule fires  →  diagnosis (Gemini, provider-only)  →  guardrails  →  LXD executes  →  audit
agents / intent  ─────────────────►  Node C front door — Gemma 3 4B on-prem (the product)
                                     / gemini-proxy → Google AI (tonight's demo: no GPU)
```

The **diagnosis** — the decision step that names the remediation — runs on the controller's
**provider-only listener** (`http://127.0.0.1:8085`, `backend=gemini-proxy` →
`aiplatform.googleapis.com`, answered by `gemini-2.5-flash`). Its substituted prompt is
**C0 telemetry only** (rule/metric context plus the instance inventory — no workload
payloads), and the user decided on 2026-10-15 that this prompt may leave the premises as part
of the managed service. This is the one call that leaves the premises, and it is labelled,
not hidden: the audit row records `egress=external destination=aiplatform.googleapis.com`.
The **agents and the intent path** stay on-prem: they call Node C's `:8080` (`backend=local`,
alias **`gemma` = Gemma 3 4B IT**, Google's open weights, CPU-only), which answers from that
node's own weights and offers no forwarded backend. The model's choice is constrained three ways — a **GBNF
grammar** under which an off-allowlist command has no valid encoding, a **param/guardrail
validator**, and a **confidence gate** — with a **mandatory deterministic fallback** that
records both the fallback and its reason. Which decider acted is recorded per-decision in
`decision_source`, so "the AI did it" is auditable rather than asserted.

Governance holds throughout: the call is wrapped by `externalModelCall()`
(`governance.md` §7.2/§9.3), which asserts the static template version and the C-class of
every substituted field before sending, and audits before/after.
**C2 (application logs, payloads, prompts, credentials) never reaches either model.**
**Note on latency:** served on Node C's CPU weights, a decision is *minutes*, not seconds
(≈84 s at a one-sample evidence window, ≈6.7 min at the real 20-sample window; the real
request is 12,748 prompt tokens under Gemma's tokenizer and fits `--ctx-size 16384`). The
diagnosis therefore runs on the provider-only Gemini surface, where the same decision is
**seconds** (measured 1.8 s warm / 13 s cold in the 2026-10-15 acceptance window). The
on-screen demo moment is still carried by the deterministic fast-path. The
Gemma-on-Cloud-Run-GPU service (`termini-gemma`) remains deployed as the reference managed
path, but it is **not** the shipped decider — the shipped diagnosis is Gemini, and the
shipped agent/intent model is Node C's on-prem Gemma.

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
| Meaningful use of Gen AI | §4 — the model *decides* the remediation; it is not a chatbot bolted onto a dashboard. Deck slide 16 (Gemini decides, Gemma stays home). |
| Technical implementation | Live GCP deployment (§3); repo test suite; `docs/architecture.md`; acceptance commands in the README. |
| Scalability & sustainability | Deck slide 17 (roadmap) and slide 12 (how it lands); Fleet HA/storage path; model-agnostic serving — the product is ops + governance, not the weights. |
| Problem alignment | §1 and deck slides 2–6 — the constraint across eight industries, the unclosed ecosystem, the three gates (MIT NANDA, McKinsey) and the stolen-data cases; operational data → **actions**, not just insights. |
| Potential impact | §1 "Who benefits". |
| Originality & differentiation | Deck slide 6 — same delegation as SaaS observability, opposite data flow, with Datadog and Samsung × Mistral as evidence that the boundary is already being bought at the top of the market; slide 7 — the plural-intelligence thesis, argued and conceded on slide 9. |
| Innovation in Gen AI use | Deck slide 11 — grammar-constrained decoding as the safety mechanism: the model *can't* emit an out-of-policy action, rather than being filtered after the fact. |
| Usability & accessibility | Deck slide 10 — bare node to running agents with no install and no specialist; browser-first dashboard; SSH access via short-lived certificates, no passwords to manage. |
| Experience & integration | One loop spanning bootstrap → telemetry → decision → execution → audit, on one screen. |

---

## 7. Repo map (where the substance lives)

| Path | What |
|---|---|
| [`README.md`](README.md) | Product + deployed-stack overview, quickstart, verification |
| [`deck/v2/`](deck/v2/index.html) | Proposal deck v2.2 — 18 slides, HTML source + CDP-rendered PDF (`termini-deck-v2.2.pdf`); v1 kept at `deck/index.html` |
| [`docs/architecture.md`](docs/architecture.md) | Frozen architecture, contracts index, R1–R24 answers |
| [`docs/contracts/governance.md`](docs/contracts/governance.md) | The C0/C1/C2 boundary and the external-model rule |
| [`docs/contracts/operator-decision.md`](docs/contracts/operator-decision.md) | The decision step's contract |
| [`docs/video-script-3min.md`](docs/video-script-3min.md) | 3-minute video script (required deliverable) |
| [`infra/cloudrun/`](infra/cloudrun/README.md) | Reproducible GCP deployment |
| [`controller/`](controller/README.md), [`termini/decision/`](termini/decision) | Controller, operator loop, model adapter |
| [`chaos/`](chaos/README.md) | Fault-injection harness |
