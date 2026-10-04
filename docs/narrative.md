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

Termini inverts the data direction: *same* delegation of operations, *opposite* data flow. Where Datadog exfiltrates telemetry to a SaaS, Termini runs a **self-hosted remediation loop** — telemetry → threshold rule → pre-approved playbook action → audited LXD execution — with intent extraction on the customer's own GPU and only C0 hardware/OS telemetry ever leaving. Never say "we replace Datadog"; say: *"Datadog proved ops can be delegated — but it does it by moving your telemetry to their cloud. We do the same job for people who can't send anything off-prem."*

### vs Samsung × Mistral (on-site AI)

**What it is:** Samsung's Sept 2026 strategic partnership with Mistral to build custom **on-premises** AI models for semiconductor design and manufacturing (Samsung also led Mistral's Series D). The stated driver is governance: keeping "national core technology" data inside Samsung's own infrastructure.

**Where Termini wins:** it is the single most citable proof our thesis is real — the world's largest memory-chip maker is paying to keep AI on-prem for data-sovereignty reasons. But it is a **bespoke, equity-backed, mega-corp engagement** that a small business cannot replicate, and it covers **models and platform, not maintenance and operations** — the deal does not answer who runs the cluster day-2. Samsung has expert engineers; SMBs do not.

Termini productizes, for the long tail, what Samsung bought bespoke: open-weight self-hosted models (we serve DeepSeek-R1 distills on the customer's GPU) **plus** the bootstrap and the governed operator that the deal leaves out. Model choice is a config knob — the product is the ops + governance layer, not the weights.

---

## 4. The 3 proof points the demo shows

**1. Bare metal to an AI-ready fleet in seconds.** A stock Ubuntu node boots the pre-baked image, registers to the controller in **under 5 seconds**, and joins the LXD cluster with its role assigned (compute vs GPU inference) — no SRE, no manual config.

**2. Natural-language intent, guardrail-enforced.** The presenter types *"deploy 3 worker agents for coding."* An on-prem model extracts a structured allocation; the deterministic guardrail clamps any impossible spec (`vcpus: 128` → host ceiling) and returns a validated, clamped plan — field by field, visible in the audit record.

**3. Governed self-healing with a data boundary.** OOM a VM live. Telemetry crosses a threshold, a deterministic rule selects a pre-approved playbook action (`restart` the runaway workload), and the action lands with a full audit trail — while a C2 field injected into the stream is rejected by the controller-side governance gate **before any byte is stored or forwarded.**

---

## 5. Tagline (recurring)

**"Your hardware. Your data. Our operations."**

---

## 6. Guardrails — what we do NOT claim

- We do **not** claim "zero data ever leaves." The enforceable claim: *no C2 customer data egresses; only allowlisted C0 hardware/OS telemetry and transformed C1 identifiers leave, and only after a fail-closed governance gate* (`governance.md` §3–§6).
- We do **not** claim a machine-learning model proposes or selects remediations in the live loop. The day-2 operator is a **deterministic threshold rule over frozen playbooks** (restart on OOM, stop on disk failure); there is no model call in the remediation path.
- We do **not** claim the demo launches VMs from natural language. The allocation path ends at a **validated, clamped plan** — `apply` returns a provisioning job, and LXD materialization is the node channel's scope (not shown live).
- We do **not** claim hotplug resize or node drain/migration. The operator **restarts** the runaway workload on OOM and **stops** the heaviest workload on disk failure; `vm.resize` and `node.drain` are allowlisted for the human dashboard path, not issued by the operator loop.
- We do **not** claim browser-generated keys or a CA-signed short-lived SSH certificate. Access is a **controller-held key** surfaced as a web terminal and an `ssh` string.
- We do **not** claim an on-node redactor that drops C2 fields. The governance gate is **controller-side**: a non-conforming telemetry batch is rejected fail-closed (422), not field-dropped.
- We do **not** claim Datadog "can't" monitor on-prem — it does, via its agent. We claim it is SaaS-architected with no self-hosted/air-gapped AI offering.
- We do **not** claim the Samsung × Mistral deal "failed" or "proves ops is unsolved." We claim only: *maintenance and operations remain open and under-served*, and the deal is bespoke, not productized.
