# Termini AI-Infra — Live Demo Narration Script

- Owner: `pitch` (kanban task `t_e0f11921`)
- Date: 2026-10-04
- Audience: the presenter, on stage, live.
- Feeds: `t_d56328e7` (red-team review of the script) and `t_a9355a73` (final W8 sign-off).
- Source of truth for claims: `docs/narrative.md`, `docs/spec-answers.md`,
  `docs/architecture.md` (§4 sequence flows, §5 trust boundaries, §8 R1–R24),
  `docs/contracts/controller-api.md`, `docs/contracts/dashboard.md`,
  `docs/contracts/governance.md`.

---

## 0. Slot, pacing, and how to read this script

**Presentation slot (live demo segment): 6:00 (360 s).** This is the assumed
slot for the live portion, after the deck hands off and before the results
slides. If the real slot differs, the per-beat table below is additive — scale
the whole thing up or down by cutting or extending beats; the fallback lines and
the wrap stay intact.

**Target: ≤ 5:00 (300 s) of narration + waits, leaving ≥ 1:00 of margin.**

- Speaking pace: measured at **~133 wpm** in the timed dry-run (§3) — a
  deliberate, presenter-read pace. Faster readers gain time; slower readers
  still have the 1:13 headroom below.
- Fixed action waits are counted separately from spoken words.
- The tagline is spoken three times (open, mid, close); it is the through-line.

### Cue legend

| Cue | Meaning |
|-----|---------|
| `[CLICK]` | Presenter acts — click, type, or trigger the named thing in the dashboard/terminal. |
| `[TALK]` | Presenter speaks the quoted line. |
| `[WAIT n]` | Hold up to `n` seconds while the system works; the attached bridge line covers the silence. |
| `[HOLD n]` | Deliberate silent beat (let it land). |
| `[FALLBACK]` | If the step misbehaves, switch to this line — ~10 s, ends on a pivot back to the story. |

---

## 1. The demo arc (6 beats)

Running total is cumulative against the 6:00 slot. Planned times are the
presenter's on-stage budget, calibrated against the measured dry-run in §3.

| Beat | What it proves | Planned | Running |
|------|----------------|---------|---------|
| 0 | Handoff from the deck — set expectations | 0:35 | 0:35 |
| 1 | Proof 1: bare metal → AI-ready fleet (registers in 150 ms; budget 5 s) | 0:40 | 1:15 |
| 2 | Proof 2: NL intent + guardrail clamp + validated plan | 1:05 | 2:20 |
| 3 | Cross-node inference, no tunnel on this leg | 0:40 | 3:00 |
| 4 | Proof 3: governed self-healing (OOM) + C2 gate | 1:15 | 4:15 |
| 5 | Operator audit + SSH + tagline wrap | 0:35 | 4:50 |

**Planned total: 4:50, leaving 1:10 margin under 6:00.**

---

### Beat 0 — Handoff from the deck  `[planned 0:35]`

**On screen:** the deck's "What you are about to see" slide (the three proof points).

- `[CLICK]` advance from the deck to the live demo view.
- `[TALK]` "The deck made a promise. The next six minutes prove it, live, on
  real hardware. Three things, in order: a node that turns bare metal into an
  AI-ready fleet in under five seconds; a plain-English request that becomes
  a validated, clamped plan — with the guardrail deciding, not the model; and a self-healing
  loop where the operator finds a fault, fixes it, and your data never leaves — with
  one disclosed substitution tonight, which I'll point at when it happens.
  If anything goes quiet on me, I'll say what I'm doing and why."
- `[FALLBACK]` (stage/timer scramble) "While we load, here's the one line that
  matters: your hardware, your data, our operations. The rest is just showing
  the work." → pivot into Beat 1.

---

### Beat 1 — Proof 1: bare metal → AI-ready fleet  `[planned 0:40]`

**On screen:** dashboard → nodes panel; Node A booting.

- `[CLICK]` open the dashboard, nodes list.
- `[TALK]` "Here's the dashboard, already talking to the controller in the
  cloud. Watch Node A come online. Stock Ubuntu, pre-baked image — nothing is
  downloaded or installed at boot."
- `[CLICK]` trigger Node A boot.
- `[TALK]` "Cloud-init finishes, the node phones home with a single-use
  token, and the controller assigns its role. That's the whole day-zero story —
  no SRE, no manual config."
- `[WAIT 5]` bridge line: "Registration is the first gate — under five seconds,
  or it didn't happen."
- `[CLICK]` show node status → `registered`, role → `compute`.
- `[TALK]` "Registered, compute role assigned, joined to the LXD cluster.
  Proof point one: day-zero, done."
- `[FALLBACK]` (node fails to register) "The token didn't land — that's the
  fail-closed path doing its job: no identity, no join. I'll re-issue and
  re-provision in a second. What matters is what the happy path just showed:
  role assignment and cluster join with no human in the loop." → pivot to Beat 2.

---

### Beat 2 — Proof 2: natural-language intent + guardrail clamp  `[planned 1:05]`

**On screen:** dashboard → intents panel.

- `[CLICK]` focus the intent box.
- `[TALK]` "Now the fun part. I type a plain-English request and we watch it
  become a validated plan."
- `[CLICK]` type: `deploy 3 worker agents for coding, plus a fast LLM`.
- `[TALK]` "Three worker agents, plus a fast LLM. Our product runs this model
  on-prem, on Node C, so data never leaves. Tonight the GPU that node would use
  isn't available to us, so the demo proxies this one call to Google's managed
  model — and the answer tells you which one answered, in-band. What comes back is
  a structured allocation: three agent VMs, plus an inference reference to Node C's
  host service."
- `[CLICK]` submit; show the extracted JSON (agent_vm ×3 + inference_vm → host_service).
- `[TALK]` "The model recommends. Then the guardrail decides. Watch what happens
  when I ask for the impossible."
- `[CLICK]` type: `128 vCPU, 512 GB RAM worker`.
- `[TALK]` "A 128-core, half-terabyte worker — the physical host has nowhere near
  that. The validator clamps it down to what the hardware can actually honor,
  field by field, and the change is visible in the audit record."
- `[CLICK]` show the clamped plan and the per-field `from → to`.
- `[TALK]` "Clamped, not crashed. That's the difference between a demo and a
  production system."
- `[CLICK]` apply; show the accepted plan returns a provisioning job.
- `[TALK]` "A validated plan, field by field, in the audit record. Proof point two."
- `[FALLBACK]` (intent extraction returns junk) "The extractor emits JSON via a
  `json_object` response format, so a bad parse can't reach the cluster — the guardrail
  rejects it outright. Let me re-prompt; the point stands: the model can
  recommend anything, the validator decides everything." → pivot to Beat 3.

---

### Beat 3 — Cross-node inference, no tunnel on this leg  `[planned 0:40]`

**On screen:** web terminal inside an agent VM on Node A.

- `[TALK]` "One thing the spec forced us to prove: no tunnel on the inference leg. I'll
  open a shell inside an agent VM on Node A and reach the
  inference box on Node C directly."
- `[CLICK]` open web terminal → agent VM on Node A.
- `[CLICK]` type: `curl http://10.10.0.13:8080/v1/models`
- `[TALK]` "The model list, served over plain HTTP on the underlay IP — no
  tunnel on this leg, no MTU black holes. And a streamed completion, tokens
  flowing Node A to Node C."
- `[CLICK]` run a streamed chat completion; show tokens streaming.
- `[TALK]` "One static route. It works, or it doesn't — there's nothing to
  misconfigure."
- `[FALLBACK]` (stream stalls) "If the stream stalls, that's the point of the
  direct route — no tunnel to misconfigure on this leg. I'll re-run the route check:
  three lines, it ends in PASS." → pivot to Beat 4.

---

### Beat 4 — Proof 3: governed self-healing + C2 gate  `[planned 1:15]`

**On screen:** telemetry chart + operator log stream.

- `[TALK]` "Now the part that makes this an operations product, not a dashboard.
  I'm going to break something — on purpose."
- `[CLICK]` launch chaos: `stress-ng --vm` inside an inference VM.
- `[TALK]` "I'm starving an inference VM of memory. Watch the telemetry —
  one-second samples, not the usual fifteen."
- `[CLICK]` show `ram_used_bytes` climbing past 95%.
- `[TALK]` "Ninety-five percent, two consecutive samples — a frozen threshold
  rule fires. The model reads that telemetry and picks the action: restart the heaviest workload."
- `[CLICK]` show operator log: `oom-guard-01` → `instance.restart`.
- `[TALK]` "The rule only *detects* — the decision is made by a managed model — Gemini on
  Vertex AI — with C0 telemetry only. It can only choose from our allowlist: a command
  outside it has no valid encoding, so the model *cannot* emit it. Then the action
  executes against LXD and lands in the audit trail."
- `[CLICK]` show `instance.restart` acked `succeeded`; the workload recovers.
- `[TALK]` "Restarted, restored, and every step is in the audit log."
- `[CLICK]` inject a C2 field into the outbound telemetry stream.
- `[TALK]` "And the boundary: I'm injecting a customer-data field into the
  outbound stream. The controller-side governance gate catches it and rejects
  the batch fail-closed — before any byte is stored. Only
  hardware and OS telemetry — C0 — is ever allowed out. Watch it get rejected."
- `[CLICK]` show the C2 field rejected (`422 GOVERNANCE_REJECT`).
- `[TALK]` "Your data stays. Our operations. Proof point three."
- `[FALLBACK]` (operator loop stalls) "The loop is quiet because the rule hasn't
  crossed its threshold — that's the guardrail refusing to act on noise. I'm
  re-driving the deterministic chaos ramp, which is exactly how we keep this
  reproducible." → pivot to Beat 5.

---

### Beat 5 — Operator audit + SSH + tagline wrap  `[planned 0:35]`

**On screen:** operator audit view; SSH/web-terminal.

- `[TALK]` "Two receipts before I hand back. First, the operator audit — every
  remediation action, traceable."
- `[CLICK]` `GET /v1/operator/audits` → show the remediation records.
- `[TALK]` "Every fix is in the log, and it's checkable, not asserted —
  a judge can pull this exact endpoint."
- `[CLICK]` open the web terminal / show the `ssh` string.
- `[TALK]` "Access is browser-first: your keypair is generated in the browser,
  only the public half uploaded, and the controller's CA signs a short-lived,
  principal-bound cert — one click, one ssh string, no passwords.
  Your hardware. Your data. Our operations."
- `[TALK]` "Back to the results."
- `[FALLBACK]` (audit endpoint slow) "The audit is a real query over the live
  records — if it's slow, the dashboard already told the same story. Zero C2
  egress is the invariant; we just watched the gate reject it live."
  → pivot to results slides.

---

## 2. Contingency appendix — 10-second fallbacks

Each line is deliverable in ~10 s at demo pace and ends on a pivot back to the
story. The four named failure modes are first; secondary modes (also named in
the source docs) follow.

| # | Failure mode | The ~10 s line (then pivot) |
|---|--------------|------------------------------|
| C1 | **Node fails to register** | "The token didn't land — that's the fail-closed path doing its job: no identity, no join. Re-issuing now. The point stands: in the happy path there is no human in the loop." → back to Beat 2. |
| C2 | **Agent loop stalls** | "The operator is quiet because the rule hasn't crossed its threshold — the guardrail refuses to act on noise. I'm re-driving the deterministic chaos ramp to make the remediation reproducible." → back to Beat 4/5. |
| C3 | **Dashboard latency** | "The dashboard is lagging the live cluster — a UI refresh, not a control-plane problem. The nodes and controller keep working; the charts will catch up. Meanwhile, the terminal is the real-time view." → back to Beat 3. |
| C4 | **VM slow to boot** | "A VM is taking its time to become task-ready. We target under ten seconds; if it drifts, the pre-warmed image is still there — nothing is downloading. I'll show the agent registering while we wait." → back to Beat 2. |
| C5 | OOM-killer beats the poll (kernel kills `stress-ng` before 95% × 2) | "That's the honest race we documented: the kernel OOM-killer sometimes wins. We re-drive the deterministic chaos ramp instead, so the detect → decide → execute → audit loop still runs end to end." → back to Beat 4. |
| C6 | Inference `503` / model unavailable | "The inference box is returning a busy signal — that's the degrade path, never a wrong-but-plausible answer. The router holds the slot; if it can't, the flash alias on the same node takes over." → back to Beat 3. |
| C7 | Governance-gate anomaly (unclassifiable field) | "If the governance gate sees anything it can't classify, it fails closed — the batch is rejected. You just saw the healthy case; the anomaly case is simply: no egress, plus a rejection record." → back to Beat 5. |
| C8 | Cross-node stream stalls | "If the stream stalls, that's the point of the direct route — no tunnel to misconfigure on this leg. Re-running the route check: three lines, it ends in PASS." → back to Beat 4. |

---

## 3. Timed dry-run — actual vs planned

**Method.** The full narration (all `[TALK]` lines, 555 words) was read aloud by
a TTS engine (edge) and measured with `ffprobe` at a pace of **~132.6 wpm** — a
real spoken reading, not a word-count estimate. Per-beat spoken time applies that
measured rate to each beat's word count (recomputed after the SSH CA + fast-LLM
re-advance). Action time counts `[CLICK]` at
2 s each plus every `[WAIT n]`/`[HOLD n]` at face value. The reproducible
word-count harness lives at `scripts/timing/demo-dry-run.py`.

| Beat | Planned | Spoken (measured) | Actions | Actual total | Δ |
|------|---------|-------------------|---------|--------------|---|
| 0 Handoff | 0:35 | 0:36 (35.7 s) | 0:02 | 0:38 | +0:03 |
| 1 Register | 0:40 | 0:30 (30.3 s) | 0:11 | 0:41 | +0:01 |
| 2 NL intent | 1:05 | 0:58 (57.5 s) | 0:12 | 1:10 | +0:05 |
| 3 Cross-node | 0:40 | 0:34 (33.9 s) | 0:06 | 0:40 | 0:00 |
| 4 Self-heal | 1:15 | 1:01 (60.6 s) | 0:12 | 1:13 | −0:02 |
| 5 Wrap | 0:35 | 0:33 (33.0 s) | 0:04 | 0:37 | +0:02 |
| **Total** | **4:50** | **4:11 (251.1 s)** | **0:47** | **4:58** | **+0:08** |

**Verdict: FITS.** Actual total **4:58** against the **6:00** slot leaves
**1:02 of margin (17%)**, and sits under the 5:00 target. No trimming was
required; the re-advanced narration (richer SSH CA + fast-LLM claims) landed
within 8 s of the planned read.
Every demo step (beats 0–5) has a matching narration beat and an inline
`[FALLBACK]` plus its appendix entry, so the presenter never goes silent.
