# Termini AI-Infra — Product Narrative & Competitive Positioning

- Owner: `pitch` (kanban task `t_1b2cab94`)
- Feeds: `t_61f40a46` (deck), `t_e0f11921` (demo script), `t_d56328e7` (red-team review)
- Source of truth for claims: `termini/project.md`, `docs/architecture.md`, `docs/contracts/governance.md`, `docs/research/04-prior-art-positioning.md`

---

## 1. Positioning statement (one line)

**Termini AI-Infra turns bare, on-premise hardware into a self-operating AI fleet — a cloud control plane bootstraps it, a governed AI operator runs it, and your data never leaves.**

---

## 2. The problem it solves

Enterprises that hold governance-sensitive data — customer records, source code, national-core IP — cannot ship that data to a SaaS cloud to get AI. They are stuck at three points no vendor has productized into one thing:

1. **Day-0 — turning hardware on.** Standing up an AI-ready cluster (hypervisor, GPU serving, networking, agent VMs) today requires a specialist. A small or regulated business has no SRE bench to do it.
2. **Day-2 — keeping it alive.** Once it runs, someone must watch disks, memory, and GPU health and fix failures. On-prem means *you* are the operations team.
3. **Governance — proving data stays.** "Your data never leaves" has to be an *enforceable property*, not a slogan in the sales deck.

Termini's wedge is the missing middle: the controller bootstraps the hardware on day-0, an AI operator remediates it on day-2, and the whole loop is gated by a fail-closed governance boundary that makes data residency checkable — not asserted.

---

## 3. Differentiation

### vs Datadog (infra ops)

**What Datadog does:** best-in-class day-2 observability — metrics, traces, logs — plus an agentic AI surface (Watchdog + Bits AI) that detects anomalies, investigates root cause, and can apply remediations. It *monitors on-prem infrastructure through its agent*.

**Where Termini wins:** Datadog is SaaS-architected — telemetry, traces, and logs ship to *its* cloud, and it has **no self-hosted / air-gapped AI offering** (FedRAMP customers are excluded from AI Credits; an entire market of "on-prem Datadog alternatives" exists for exactly this reason). Datadog also assumes the infrastructure *already exists*: no hardware bootstrapping, no VM provisioning, no day-0 story. And its remediation runs in its cloud, not on the customer's metal.

Termini inverts the data direction: *same* delegation of operations, *opposite* data flow. Where Datadog exfiltrates telemetry to a SaaS, Termini runs a **self-hosted remediation loop** — telemetry → threshold rule → model-chosen action → audited LXD execution — with intent extraction on the customer's own hardware and only C0 hardware/OS telemetry ever leaving. Never say "we replace Datadog"; say: *"Datadog proved ops can be delegated — but it does it by moving your telemetry to their cloud. We do the same job for people who can't send anything off-prem."*

### vs Samsung × Mistral (on-site AI)

**What it is:** Samsung's Sept 2026 strategic partnership with Mistral to build custom **on-premises** AI models for semiconductor design and manufacturing (Samsung also led Mistral's Series D). The stated driver is governance: keeping "national core technology" data inside Samsung's own infrastructure.

**Where Termini wins:** it is the single most citable proof our thesis is real — the world's largest memory-chip maker is paying to keep AI on-prem for data-sovereignty reasons. But it is a **bespoke, equity-backed, mega-corp engagement** that a small business cannot replicate, and it covers **models and platform, not maintenance and operations** — the deal does not answer who runs the cluster day-2. Samsung has expert engineers; SMBs do not.

Termini productizes, for the long tail, what Samsung bought bespoke: open-weight self-hosted models (we serve **Gemma**, Google's open-weights model, on the customer's own nodes) **plus** the bootstrap and the governed operator that the deal leaves out. Model choice is a config knob — the product is the ops + governance layer, not the weights.

---

## 4. The 3 proof points the demo shows

**1. Bare metal to an AI-ready fleet in seconds.** A stock Ubuntu node boots the pre-baked image, registers to the controller in **under 5 seconds**, and joins the LXD cluster with its role assigned (compute vs inference) — no SRE, no manual config.

**2. Natural-language intent, guardrail-enforced.** The presenter types *"deploy 3 worker agents for coding — plus a fast LLM."* An on-prem model extracts a structured allocation (three `agent_vm`s and one `inference_vm`, which resolves to the always-on Node C inference service rather than a launched VM); the deterministic guardrail clamps any impossible spec (`vcpus: 128` → host ceiling) and returns a validated, clamped plan — field by field, visible in the audit record.

**3. Governed self-healing with a data boundary.** OOM a VM live. Telemetry crosses a threshold, the model selects the action from the allowlist (`restart` the runaway workload), and the action lands with a full audit trail — while a C2 field injected into the stream is rejected by the controller-side governance gate **before any byte is stored or forwarded.**

---

## 5. Tagline (recurring)

**"Your hardware. Your data. Our operations."**

---

## 6. Guardrails — what we do NOT claim

- We do **not** claim "zero data ever leaves." The enforceable claim: *no C2 customer data egresses; only allowlisted C0 hardware/OS telemetry and transformed C1 identifiers leave, and only after a fail-closed governance gate* (`governance.md` §3–§6).
- We do **not** claim the model's choice is reproducible, nor that the model *detects* the fault. A threshold rule detects (OOM, disk failure); the day-2 operator is **model-driven** — the model selects the action from an allowlist that grammar-constrained decoding makes unbreachable, with a deterministic provider as the mandatory fallback (`governance.md` §7.3). What is guaranteed is the safety envelope, not the specific action, and only C0 telemetry is in the call.
- We do **not** claim the demo launches VMs from natural language. The allocation path ends at a **validated, clamped plan** — `apply` returns a provisioning job, and LXD materialization is the node channel's scope (not shown live).
- We do **not** claim hotplug resize or node drain/migration in the operator loop. The operator **restarts** the runaway workload on OOM and **stops** the heaviest workload on disk failure; `vm.resize` and `node.drain` are allowlisted for the human dashboard path and the assistant endpoint (`controller-api.md` §4.10, which executes them on guardrail admission with `initiated_by: assistant`) — not issued by the operator loop.
- We do **not** claim long-lived or shared SSH credentials. Access is a **controller-held SSH CA**: the operator generates a keypair in the browser, uploads only the public half, and the controller signs a **short-lived (≤8 h), principal-bound OpenSSH certificate** — surfaced as a web terminal and an `ssh` string, revocable before TTL.
- We do **not** claim an on-node redactor that drops C2 fields. The governance gate is **controller-side**: a non-conforming telemetry batch is rejected fail-closed (422), not field-dropped.
- We do **not** claim Datadog "can't" monitor on-prem — it does, via its agent. We claim it is SaaS-architected with no self-hosted/air-gapped AI offering.
- We do **not** claim the Samsung × Mistral deal "failed" or "proves ops is unsolved." We claim only: *maintenance and operations remain open and under-served*, and the deal is bespoke, not productized.
- We do **not** claim GPU-accelerated inference on the customer's node. The **agent-facing**
  model runs on Node C, CPU-only (`llama.cpp`, no `guest_accelerator` on the instance —
  `gpu_accelerator_count = 0`); GPU acceleration is a config knob for paid/bare-metal
  accounts, not shipped live. The operator's **diagnosis** runs on a **managed model
  service** (Gemini on Vertex AI) reached from the controller — an external call, disclosed,
  not a GPU on the customer's node.
- The model on Node C is **Gemma 3 4B IT** — Google's own open-weights model, served by
  `llama.cpp` under the alias **`gemma`** from the pinned Q4_K_M GGUF. It is **not** a
  DeepSeek distill (a Gemma GGUF is pinned in `image/models.lock.json` and delivered to the
  node from the project's own bucket).
- We do **not** claim the model-driven decision is *interactive*. The **diagnosis** (the
  shipped decision path) runs on the provider-only Gemini listener and returns in **seconds**
  (measured 1.8 s warm / 13 s cold in the 2026-10-15 window). Served on Node C's CPU weights
  (`n2-standard-8`, pinned `gemma` = Gemma 3 4B IT Q4_K_M, 8 threads, `--ctx-size 16384`),
  the same prompt takes **~84 s with a one-sample evidence window and ~6.7 min (402 s) with
  the real 20-sample window** (prefill ≈33 tok/s, generation ≈8–10 tok/s); the real 20-sample
  request is **12,748 prompt tokens** under Gemma's tokenizer and **fits** the shipped context.
  That gap is exactly why the diagnosis runs on the managed service while the agent path stays
  on Node C. The on-screen demo moment is covered by the **deterministic fast-path**
  (`architecture.md` R24), which fires in milliseconds. (Node C replaced a DeepSeek-R1 distill
  that was slower still — 156 s / 12 min — and whose 8,973-token request did not even fit the
  shipped context.)
- We do **not** hide the `inference` role assignment. Node C runs the model CPU-only, but the demo harness reports a synthetic 24 GiB VRAM footprint from config (`gpu_bytes` → `gpu_vram_bytes`) so the real role rule (≥12 GiB VRAM → `inference`) is exercised end-to-end. The synthetic report drives role selection only; it does not claim a physical GPU is present on the build host.

---

## 7. Deck v2 — the story-led arc, its sources, and what is thesis

`deck/v2/termini-deck-v2.pdf` replaces the v1 framing (*AI is stuck at the on-prem
gate*) with a story-led arc that starts from one clinic and ends at the architecture.
Three classes of statement must not be confused when this deck is edited.

### 7.1 External sources (cited on the slide that uses them)

| Source | What the deck uses it for | How to read it |
|---|---|---|
| “A neighbourhood dental clinic's AX transformation”, arca.live `b/aihomelab/185810092` (13 Oct 2026; 330k+ views) | The whole Act 1 story: 12 DGX Spark-class boxes wired in rings of four, 2× Mac mini M5 Pro + an M4 mini, 3 virtualisation servers, 6 UPS, a self-built PBX, first token in 0.1 s, insurance-claim reconciliation, the monthly three-model diagnosis, the human grading screen, accountant/SaaS cancelled, marketing at zero, and the line “cheaper than the salary of a bad hire” | **One public forum post.** It is cited as a case, never as a market size. Its figures are the author's own report, not audited |
| MIT NANDA, *The GenAI Divide: State of AI in Business 2025* (Jul 2025) | 95% of enterprise GenAI pilots with no measurable P&L impact; $30–40B of spend; ~5% reaching production | **Preliminary and not peer-reviewed**; the 95% has been publicly contested. The deck labels it “preliminary, contested” and uses it as directional, not as a verdict |
| McKinsey, *The State of AI 2025* | 88% of organisations using AI in at least one function; 6% high performers | Survey of self-reported use; the deck quotes the two figures, not an ROI claim |

### 7.2 Thesis (labelled as a thesis on slide 7, argued and conceded on slide 9)

- Intelligence should be **plural**: one model per entity, over its own data, beats one
  model over everyone's data — because value scales with context and context cannot be
  uploaded.
- Centralisation correlates failure (same weights → same blind spots), so a population of
  sovereign models behaves like a portfolio; disagreement between them is a usable signal.
- Learning (evals, weights, lessons) can travel between entities even when records cannot.
- The counterargument — *isn't local just worse?* — is stated in the deck and answered
  there, including the concession that generic work should stay on the cloud.

None of §7.2 is measured. The dentist's three-model jury, eval loop and private corpus
(slide 8) are the only evidence offered for it, and they are one clinic's practice.

### 7.3 Measured claims (unchanged from v1)

Slides 13 and 15 reuse the v1 measurements and their sources: `register_ms=150`, the
128 vCPU → 8 clamp, the C2 `422` rejection, the model-decided remediation with its audit
row, the 1.8 s warm / 402 s local diagnosis timings, and the live GCP + Firebase
deployment (`SUBMISSION.md` §3–§4).

**Hard constraint on future edits:** the deck's data claim is scoped on slide 15 to
C0 / C1 / C2, and that slide discloses the one external call (the operator's diagnosis on
Vertex AI, C0-only, audited as `egress=external`, with `backend=local` as the
one-variable fallback). Do **not** restore an absolute “nothing leaves” claim anywhere in
the deck, and do not delete slide 15 without moving its content.
