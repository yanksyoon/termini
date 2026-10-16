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
| 2 | Proof 2: NL intent + guardrail clamp + validated plan + agent task API | 1:05 | 2:20 |
| 3 | Cross-node inference front door, no tunnel on this leg | 0:45 | 3:05 |
| 4 | Proof 3: governed self-healing (OOM), the incident card & **thinking log**, C2 gate | 1:35 | 4:40 |
| 5 | Operator audit + SSH + tagline wrap | 0:35 | 5:15 |

**Planned total: 5:15. Measured actual is 5:54 (§3) — inside the 6:00 slot, but
~54 s over the 5:00 narration target; §3 names which beats carry the overrun.**

---

### Beat 0 — Handoff from the deck  `[planned 0:35]`

**On screen:** the deck's "What you are about to see" slide (the three proof points).

- `[CLICK]` advance from the deck to the live demo view.
- `[TALK]` "The deck made a promise; the next six minutes prove it, live, on real
  hardware. Three things in order: bare metal to an AI-ready fleet in under five
  seconds; a plain-English request that becomes a validated, clamped plan — the
  guardrail deciding, not the model; and a self-healing loop where the operator
  finds the fault, fixes it, and shows its reasoning — one disclosed
  substitution tonight. If anything goes quiet, I'll say why."
- `[FALLBACK]` (stage/timer scramble) "While we load, here's the one line that
  matters: your hardware, your data, our operations. The rest is just showing
  the work." → pivot into Beat 1.

---

### Beat 1 — Proof 1: bare metal → AI-ready fleet  `[planned 0:40]`

**On screen:** dashboard → nodes panel; Node A booting.

- `[CLICK]` open the dashboard, nodes list.
- `[TALK]` "The dashboard, already talking to the controller in the cloud. Watch
  Node A come online — stock Ubuntu, pre-baked image, nothing downloaded at boot."
- `[CLICK]` trigger Node A boot.
- `[TALK]` "Cloud-init finishes, the node phones home with a single-use token,
  and the controller assigns its role. That's day zero — no SRE, no manual config."
- `[WAIT 5]` bridge line: "Registration is the first gate — under five seconds,
  or it didn't happen."
- `[CLICK]` show node status → `registered`, role → `compute`.
- `[TALK]` "Registered, compute role assigned, joined to the LXD cluster.
  Day-zero, done."
- `[FALLBACK]` (node fails to register) "The token didn't land — that's the
  fail-closed path doing its job: no identity, no join. I'll re-issue and
  re-provision in a second. What matters is what the happy path just showed:
  role assignment and cluster join with no human in the loop." → pivot to Beat 2.

---

### Beat 2 — Proof 2: natural-language intent + guardrail clamp  `[planned 1:05]`

**On screen:** dashboard → intents panel.

- `[CLICK]` focus the intent box.
- `[TALK]` "Now the fun part: plain English, and we watch it become a validated plan."
- `[CLICK]` type: `deploy 3 worker agents for coding, plus a fast LLM`.
- `[TALK]` "Three worker agents, plus a fast LLM. That sentence is read by a model
  on Node C itself — its own local instance, deliberately not the forwarded one —
  so it never leaves the premises. Back comes a structured allocation: three
  agent VMs and an inference reference to Node C's host service."
- `[CLICK]` submit; show the extracted JSON (agent_vm ×3 + inference_vm → host_service).
- `[TALK]` "The model recommends; the guardrail decides. Now the impossible."
- `[CLICK]` type: `128 vCPU, 512 GB RAM worker`.
- `[TALK]` "A 128-core, half-terabyte worker — the host has nowhere near that.
  The validator clamps it to what the hardware can honor, field by field, in the
  audit record."
- `[CLICK]` show the clamped plan and the per-field `from → to`.
- `[TALK]` "Clamped, not crashed."
- `[CLICK]` apply; show the accepted plan returns a provisioning job.
- `[TALK]` "A validated plan in the audit record. And the fleet is drivable: the
  agent-task API takes a task for one of those VMs and journals the answer
  against its `agent_task_id`. Proof point two."
- `[FALLBACK]` (intent extraction returns junk) "The extractor emits JSON via a
  `json_object` response format, so a bad parse can't reach the cluster — the guardrail
  rejects it outright. Let me re-prompt; the point stands: the model can
  recommend anything, the validator decides everything." → pivot to Beat 3.

---

### Beat 3 — Cross-node inference front door, no tunnel on this leg  `[planned 0:45]`

**On screen:** web terminal inside an agent VM on Node A; then the front door's own `/health`.

- `[TALK]` "The spec forced us to prove one thing: no tunnel on the inference leg. I'll
  open a shell inside an agent VM on Node A and reach Node C's inference box directly."
- `[CLICK]` open web terminal → agent VM on Node A.
- `[CLICK]` type: `curl http://10.10.0.13:8080/v1/models`
- `[TALK]` "The model list, over plain HTTP on the underlay IP — no tunnel, no
  MTU black holes. One disclosure: this is the agent-facing front door, and with
  no GPU node for Node C, tonight's demo forwards this call to Google's managed
  model. The shipped setting is `backend=gemini-proxy`; one variable,
  `backend=local`, puts it back on the node."
- `[CLICK]` run a streamed chat completion; show tokens streaming.
- `[CLICK]` show the front door's `/health`: `backend=gemini-proxy`,
  `answered_by=forwarded-managed-model`, `is_gpu_inference=false`.
- `[TALK]` "The front door names its backend in-band, so no client guesses. One
  static route: it works, or it doesn't."
- `[FALLBACK]` (stream stalls) "If the stream stalls, that's the point of the
  direct route — no tunnel to misconfigure on this leg. I'll re-run the route check:
  three lines, it ends in PASS." → pivot to Beat 4.

---

### Beat 4 — Proof 3: governed self-healing + the AI's reasoning + C2 gate  `[planned 1:35]`

**On screen:** telemetry chart, the live AI-operator log, then the incident card
with its **thinking log** expanded.

- `[TALK]` "Now the part that makes this an operations product: I'm going to break
  something, on purpose."
- `[CLICK]` launch chaos: `stress-ng --vm` inside an inference VM.
- `[TALK]` "I'm starving an inference VM of memory; watch the telemetry —
  one-second samples, not the usual fifteen."
- `[CLICK]` show `ram_used_bytes` climbing past 95%.
- `[TALK]` "Ninety-five percent, twice — the frozen threshold rule fires."
- `[CLICK]` show operator log: `oom-guard-01` → `instance.restart`.
- `[TALK]` "The rule only *detects*. The decision is Gemini on Vertex AI, C0
  telemetry only, and an off-allowlist command has no valid encoding. That log
  line is the conclusion; this card is the reasoning."
- `[CLICK]` open the AI panel's incident card.
- `[TALK]` "Title: what happened. Explanation: why the model thinks so. The
  record is contractual, bounded and C2-scanned by guardrail G9."
- `[CLICK]` expand the **thinking log** (the collapsed disclosure under the explanation).
- `[TALK]` "And between them the thinking log — `decision_trace` in the API.
  Expand it: threshold fired, evidence window, prompt hash, the model call with
  its endpoint and `answered_by`, every gate, the action, the execution, the
  audit row."
- `[CLICK]` point at the card's provenance badge.
- `[TALK]` "Provenance is in-band: `incident_generated`, and `decided_by` —
  `model` here, or `fallback` with its `fallback_reason`. Never rendered alike."
- `[TALK]` "Evidence: the telemetry fields it quoted — per-process metrics at the
  top ten by memory, journal counts per service — each with its sample sequence
  number. Resolution: what it decided, and what the fleet answered."
- `[CLICK]` show `instance.restart` acked `succeeded`; the workload recovers.
- `[TALK]` "Restarted, restored, audited."
- `[CLICK]` inject a C2 field into the outbound telemetry stream.
- `[TALK]` "And the boundary: a customer-data field into the outbound stream —
  rejected fail-closed before a byte is stored. Only C0 telemetry ever leaves.
  Watch."
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
  remediation, traceable."
- `[CLICK]` `GET /v1/operator/audits` → show the remediation records.
- `[TALK]` "Every fix is in the log, checkable not asserted — a judge can pull
  this exact endpoint."
- `[CLICK]` open the web terminal / show the `ssh` string.
- `[TALK]` "Access is browser-first: your keypair is generated in the browser,
  only the public half uploaded, and the controller's CA signs a short-lived
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
| C9 | **No thinking log on the row** (the demo build predates the trace, or a deterministic-mode decision wrote none) | "This row carries no trace — a deterministic decision writes none, and a build older than the trace cannot show one. What the card shows instead is the conclusion plus the audit row; the reasoning steps come with the controller revision that ships `decision_trace`." → back to Beat 4/5. |

---

## 3. Timed dry-run — actual vs planned

**Method.** The full narration (all `[TALK]` lines) was measured by
`scripts/timing/demo-dry-run.py` at a pace of **~132.6 wpm** — the rate that was
originally established as a real spoken reading with a TTS engine (edge) and
`ffprobe`, not a word-count guess. Per-beat spoken time applies that rate to each
beat's word count; action time counts `[CLICK]` at
2 s each plus every `[WAIT n]`/`[HOLD n]` at face value. **Re-measured
2026-10-16** on the tree that adds the thinking-log moment (task
`t_6c5e9925`); the previous table was stale — it read 4:58 while the narration on
`main` already measured **5:54** before the change.

| Beat | Planned | Spoken (measured) | Actions | Actual total | Δ vs planned | Δ vs before |
|------|---------|-------------------|---------|--------------|---|-------------|
| 0 Handoff | 0:35 | 0:32 (32.1 s) | 0:02 | 0:34 | −0:01 | −0:09 |
| 1 Register | 0:40 | 0:26 (26.2 s) | 0:11 | 0:37 | −0:03 | −0:04 |
| 2 NL intent + agent task | 1:05 | 1:02 (62.4 s) | 0:12 | 1:14 | +0:09 | −0:05 |
| 3 Inference front door | 0:45 | 0:49 (48.9 s) | 0:08 | 0:57 | +0:12 | −0:06 |
| 4 Self-heal + **thinking log** | 1:35 | 1:37 (97.3 s) | 0:18 | 1:55 | +0:20 | **+0:26** |
| 5 Wrap | 0:35 | 0:32 (31.7 s) | 0:04 | 0:36 | +0:01 | −0:01 |
| **Total** | **5:15** | **4:59 (298.6 s)** | **0:55** | **5:54** | **+0:39** | **+0:00** |

### What the thinking-log moment costs

The requirement was that the moment must not add *minutes*. Measured, per step:

| Step in Beat 4 | Added |
|---|---|
| the three extra `[CLICK]`s (open the card, expand the log, point at provenance) | +6 s |
| the panel narration (incident record → `decision_trace` steps → `decided_by`/`fallback_reason` → evidence/resolution) | +20 s |
| **Beat 4 total** | **+26 s** |
| reclaimed from redundant narration in beats 0–3 and 5 (no claim dropped) | −26 s |
| **net for the whole demo** | **≈ 0 s** |

**Verdict: the thinking log costs ~26 s of Beat 4 and nothing net** — it is paid
for out of narration that repeated the model-decides story the trace now *shows*.
The demo still fits the **6:00** slot, at **5:54** with **6 s** of margin — the
same margin the script had before this change.

**Standing shortfall, stated rather than hidden.** The harness exits non-zero
because the script is **~54 s over the ≤5:00 narration target** — it was already
**~54 s over** before this change (5:54), and this change neither adds to nor
removes that. The overrun lives in beats 2 (1:14 vs 1:05) and 3 (0:57 vs 0:45),
both of which grew when the proxy disclosure and the fast-LLM re-advance landed
without a re-measure. Trimming them is a pacing decision for the owner, not a
side effect of this card.

Every demo step (beats 0–5) has a matching narration beat and an inline
`[FALLBACK]` plus its appendix entry (C9 covers a row with no thinking log), so
the presenter never goes silent.
