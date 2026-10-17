# Termini — Your hardware. Your data. Our operations.

Termini (Termini AI-Infra) turns bare, on-premise hardware into a self-operating
AI fleet: a cloud control plane bootstraps it on day 0, a governed AI operator
keeps it alive on day 2, and your data never leaves. A small or regulated
enterprise gets an AI-ready cluster without an SRE bench — and "your data never
leaves" is an *enforceable property*, not a sales slogan
([`docs/narrative.md`](docs/narrative.md)).

---

## The problem

Enterprises holding governance-sensitive data — customer records, source code,
national-core IP — cannot ship it to a SaaS cloud to get AI. They are stuck at
three points no single vendor has productized
([`docs/narrative.md`](docs/narrative.md) §2):

1. **Day-0 — turning hardware on.** Standing up an AI-ready cluster (hypervisor,
   GPU serving, networking, agent VMs) needs a specialist today; a small or
   regulated business has no SRE bench.
2. **Day-2 — keeping it alive.** Once it runs, someone must watch disks, memory,
   and GPU health and fix failures. On-prem means *you* are the operations team.
3. **Governance — proving data stays.** "Your data never leaves" has to be
   checkable — not a slogan in the deck.

Termini's wedge is the missing middle: the controller bootstraps the hardware on
day 0, a model-driven AI operator remediates it on day 2, and the whole loop is
gated by a fail-closed governance boundary that makes data residency *verifiable*.

---

## Architecture

Three diagrams, derived faithfully from the frozen
[`docs/architecture.md`](docs/architecture.md) §2 and §4. The normative detail —
frozen addressing, trust boundaries, failure modes, and the R1–R24 reconciliation
answers — lives there.

### 1. System overview

A GCP control plane (controller VM) and the customer premises (Nodes A/B compute +
Node C CPU inference) are joined by a single **outbound-only mTLS control
channel**; the controller's **governance gate** sits on the ingest path.

```text
   ┌──────────────────────────────────────────────────────────────┐
   │  GCP control plane — controller VM (10.10.0.10)              │
   │                                                              │
   │  browser (project token)                                     │
   │    REST /v1 ───► REST API ─► guardrail / clamp validator     │
   │    /stream/v1        │                                       │
   │    /terminal/v1      ▼                                       │
   │                 AI operator loop (reconcile)                 │
   │               telemetry → threshold rule → AI-decided action │
   │                 → LXD REST (audited) → command / ack         │
   │                                                              │
   │        ▲ governance gate (fail-closed, C2 → 422 reject)      │
   └────────┼─────────────────────────────────────────────────────┘
            │  outbound-only mTLS control channel
            │  (heartbeat · telemetry · commands · acks)
   ┌────────┼─────────────────────────────────────────────────────┐
   │  customer premises — underlay 10.10.0.0/24                   │
   │                                                              │
   │  Node A (seed)      Node B (compute)     Node C (CPU)        │
   │  10.10.0.11         10.10.0.12          10.10.0.13 :8080     │
   │  LXD member         LXD member          llama.cpp / gemma    │
   │  bootstrap          bootstrap           (on-prem inference)  │
   │  agent VMs          agent VMs                                 │
   └──────────────────────────────────────────────────────────────┘
```

Roles: Node A = LXD cluster seed + compute, Node B = compute, Node C = **CPU-only** node
hosting the host-exposed inference server (`:8080`, alias `gemma` = Gemma 3 4B IT). Agent VMs run only on A/B;
there is no LXD overlay in the demo — cross-node traffic routes over the underlay.

### 2. Day-0 bootstrap flow

Power-on to a registered, role-assigned, cluster-joined node — measured at
**150 ms** from daemon start to controller acknowledgement, against the 5-second
budget ([`docs/architecture.md`](docs/architecture.md) §4.1). From VM power-on the
whole boot-to-registered path is ~18.6 s, dominated by firmware/OS/cloud-init.

```text
   power-on
     │
     ▼
   pre-baked host image (agentic-os/w3)
   cloud-init writes config.yaml + CA pubkey + single-use enrollment token
     │
     ▼
   termini-bootstrap daemon (systemd):
   sync clock (chrony) → gather hardware facts →
   generate Ed25519 keypair → send CSR
     │
     ▼
   POST /v1/nodes/register   (Authorization: Bearer <single-use token>)
     │
     ▼
   controller: validate token → upsert by machine_id → assign role
   (compute | inference) → mint client cert + node token + LXD join token
     │
     ▼
   node: persist identity → lxd init --preseed (seed first, joiners after)
   → open mTLS control channel → start telemetry
     │
     ▼
   registered in < 5 s
```

### 3. Day-2 self-healing loop

Telemetry crosses a threshold, the operator's decision step selects a
pre-approved action, and the remediation lands with a full audit trail — while a
non-conforming batch is rejected fail-closed at the governance gate
([`docs/architecture.md`](docs/architecture.md) §4.3–§4.4). The decision step is a
seam, and **the shipped demo runs it model-driven**: `oom-guard-01` and
`disk-fail-01` route through **`llama.cpp` on Node C** (CPU-only, alias
`gemma` = Gemma 3 4B IT), which picks
the action under a hard constraint stack — a GBNF grammar that makes an
out-of-allowlist command unencodable, a param/guardrail validator, and a
confidence gate — with a **mandatory deterministic fallback** that records the
fallback and its reason. The deterministic playbook provider remains the default
and the fallback; which decider chose a given action is recorded per-decision in
`decision_source` ([`docs/contracts/governance.md`](docs/contracts/governance.md) §7.3,
[`docs/contracts/operator-decision.md`](docs/contracts/operator-decision.md)).

```text
   telemetry (C0/C1, 1 s cadence)
     │
     ▼
   governance gate — controller-side, fail-closed
   (C2 detected → 422 GOVERNANCE_REJECT, nothing stored or forwarded)
     │
     ▼
   threshold rule fires  (e.g. oom-guard-01: ram_used ≥ 95% for 2 samples)
     │
     ▼
   decision step: playbook (reference) or model — allowlist-constrained
   instance.restart · instance.stop · instance.delete · image.prune
     │
     ▼
   LXD REST — audited (method / path / body + result recorded)
     │
     ▼
   command / ack ──► audit record (actor: ai_operator, trigger, rule_id)
```

---

## Repository map

Every top-level directory and its primary README. Component READMEs are the
source of truth for their scope — this document links them, it does not repeat
them.

| Directory | Purpose | README |
|-----------|---------|--------|
| [`chaos/`](chaos/README.md) | Fault-injection harness: simulates nodes, drives the frozen thresholds, records the LXD command the operator loop issues | [chaos/README.md](chaos/README.md) |
| [`console/`](console/README.md) | One-command W5 console: dashboard + web terminal against one controller | [console/README.md](console/README.md) |
| [`controller/`](controller/README.md) | Control-plane core: registration, role allocation, telemetry ingest, operator loop, SSH CA | [controller/README.md](controller/README.md) |
| [`dashboard/`](dashboard/README.md) | Customer operations dashboard: node health, AI-operator log stream, manual intervention | [dashboard/README.md](dashboard/README.md) |
| [`deck/`](deck/README.md) | Judge-facing HTML slide deck | [deck/README.md](deck/README.md) |
| [`docs/`](docs/architecture.md) | Normative specs and judge materials (architecture, contracts, narrative, spec answers, demo script) | [docs/architecture.md](docs/architecture.md) |
| [`frontend/`](frontend/README.md) | Web terminal + SSH access panel | [frontend/README.md](frontend/README.md) |
| [`image/`](image/README.md) | Pre-baked image pipeline (`agentic-os/w3`) + agent-vm image | [image/README.md](image/README.md) |
| [`infra/`](infra/bootstrap/README.md) | Node footprint (GCP), bootstrap daemon, LXD cluster, inference, network, warm layer | [infra/bootstrap/README.md](infra/bootstrap/README.md) |
| [`scripts/`](scripts/timing/README.md) | Helper scripts: chaos, timed dry-run, doc checks, the `land.sh` merge gate | [scripts/timing/README.md](scripts/timing/README.md) |
| [`termini/`](termini/intent/README.md) | NL intent extractor + clamp validator (`termini.intent`) and the project spec | [termini/intent/README.md](termini/intent/README.md) |
| [`tests/`](tests/) | Unit + integration suites (controller, operator, telemetry, SSH CA, terminal) — no README; run via CI | [.github/workflows/ci.yml](.github/workflows/ci.yml) |
| [`w7/`](w7/verdict.md) | W7 gate verdict + W1–W6 acceptance harness + red-team evidence | [w7/verdict.md](w7/verdict.md) |

`infra/` sub-projects each carry their own README:
[bootstrap](infra/bootstrap/README.md) ·
[cluster](infra/cluster/README.md) ·
[gcp](infra/gcp/README.md) ·
[images](infra/images/README.md) ·
[inference](infra/inference/README.md) ·
[net](infra/net/README.md) ·
[warm](infra/warm/README.md) ·
[agent-vm](infra/agent-vm/README.md).

`scripts/` sub-projects:
[chaos](scripts/chaos/README.md) ·
[timing](scripts/timing/README.md).

---

## Quickstart

Everything is stdlib-only (Python ≥3.10 + Node for the dashboard) — no third-party
packages, no build step.

```bash
# 1. The whole composed demo — dashboard at http://127.0.0.1:8090/
console/run-demo.sh

# 2. The controller alone (requires a single-use bootstrap token)
export TERMINI_BOOTSTRAP_TOKEN="$(openssl rand -base64 32)"
cd controller && make run          # or: python3 -m app.main

# 3. One fault scenario end to end (oom | disk-fail | healthy)
make chaos SCENARIO=oom
```

Details and environment knobs: [console/README.md](console/README.md),
[controller/README.md](controller/README.md), [chaos/README.md](chaos/README.md).

---

## Deployed stack (live on GCP)

The demo also runs **deployed**, not only composed locally. Everything below is
live in project `termini-510609` and serves the same contracts as the local
compose — same controller, same decision step, same audit trail.

> **This deployment is normally PAUSED** (the seeder is stopped and the node VMs are
> terminated) so it does not bill GPU time while nobody is watching. In that state the
> dashboard still loads but shows **offline nodes and an empty operator log** — it
> looks broken. Resume it with
> `gcloud compute ssh termini-controller --zone asia-south1-a --command "sudo systemctl start termini-seeder"`
> (~30 s); the GPU scales to zero on its own once traffic stops.
>
> The seeder **self-provisions**: it mints its own fresh single-use bootstrap tokens
> from the controller at start-up (`--bootstrap-mint`, `POST /v1/bootstrap-tokens`,
> loopback-only), so no hand-minted token is ever needed and a restart just works.
> Real nodes are unaffected — a bootstrap token is still burned on first use and a
> replayed one is still rejected.

| Component | Where | Endpoint |
|---|---|---|
| Dashboard | Firebase Hosting | `https://termini-510609.web.app` |
| Agent-facing model | **Node C** (GCE VM `termini-node-c`) — CPU-only, `n2-standard-8`, `asia-south1-a` | `http://10.10.0.13:8080` (underlay only) — the front door, shipped in `backend=gemini-proxy` since the pivot (`t_957956e5` / `t_3fcee90b`): the managed model answers and every response says so in-band (`backend`, `answered_by`, `served_model`, `is_gpu_inference:false`). `backend=local` — `llama.cpp`, alias `gemma` = Gemma 3 4B IT, that node's own weights — is the one-variable fallback |
| Provider-only diagnosis model | **same controller VM**, loopback | `http://127.0.0.1:8085` — the same proxy in `backend=gemini-proxy` → `aiplatform.googleapis.com` (`gemini-2.5-flash`). No agent route to it |
| Controller | GCE VM `termini-controller` — `n2-standard-2`, `asia-south1-a` | `https://34-93-190-64.sslip.io` |
| Telemetry seeder | systemd on the controller VM | — (fabricates `demo-node-a` / `demo-node-c`) |
| Model weights | GCS | `gs://termini-510609-models/gemma-3-4b-it-gguf/gemma-3-4b-it-Q4_K_M.gguf` (2.49 GB, sha256-pinned); `gs://termini-510609-models/gemma-3-4b-it/` holds the HF safetensors for the Cloud Run reference |
| Node image (pre-baked) | GCE image, family `termini-node-os` | carries the bootstrap daemon — a node registers with **no installs at boot** (measured `register_ms=150`) |

The operator's decision step runs **model-driven** there
(`TERMINI_OPERATOR_DECISION_MODE=model`): a fired threshold is diagnosed and
remediated by the **provider-only listener on the controller**
(`http://127.0.0.1:8085`, `backend=gemini-proxy` → `aiplatform.googleapis.com`,
answering with `gemini-2.5-flash`). The substituted prompt is **C0 telemetry
only** — rule/metric context plus the instance inventory, never workload
payloads — and the `externalModelCall()` guard asserts every substituted field is
C0 / transformed-C1 before sending, so the audit row reads
`egress=external destination=aiplatform.googleapis.com`. The model chooses the
action, the guardrail stack constrains it, and the deterministic provider remains
the mandatory fallback — which decider acted is recorded per-decision in
`decision_source`.

The **agents** are served by the other surface: Node C's `:8080`
(`n2-standard-8`, CPU-only, pinned `gemma` = Gemma 3 4B IT Q4_K_M) over the
underlay. Since the pivot (`t_957956e5` / `t_3fcee90b`) that front door ships
`backend=gemini-proxy` — the same forwarded managed model, labelled in-band in
every response — because the live-GPU path is blocked by the L4 quota and this
node's own CPU weights are slow (generation ≈8–10 tok/s; the operator's 20-sample
prompt measured 402 s when it ran against them). `backend=local` remains the
one-variable fallback and is that measured path.

**Measured on Node C: a local decision is minutes, not seconds.** A decision over the
operator's real prompt takes **≈84 s at a one-sample evidence window and ≈6.7 min
(402 s) at the real 20-sample window** (prefill ≈33 tok/s, generation ≈8–10 tok/s);
the real 20-sample request is **12,748 prompt tokens** under Gemma's tokenizer and
**fits** the shipped context (`--ctx-size 16384 --parallel 1`). That is the number
behind the pivot: the shipped **diagnosis** runs on the controller's provider-only
surface (seconds), and the agent-facing front door now ships the forwarded backend
rather than the 402-second one — the locally-served path is kept as the on-prem
fallback and says so in-band when it is selected. The interactive on-screen moment
is still carried by the **deterministic fast-path**. (It replaced a DeepSeek-R1
distill that was slower still — 156 s / 12 min, and whose 8,973-token request did
not fit the old shipped context.)

Two deliberate departures from the "everything on the customer's metal" shape,
both recorded rather than implied:

- **The agent path runs on-prem on Node C, CPU-only — and the latency is the
  trade.** Node C has no accelerator (`n2-standard-8`), so agent work served from
  its weights costs seconds-to-minutes; an L4 would bring it to seconds but is
  deferred (the project's free-tier billing account rejects every non-TPU
  accelerator). The **diagnosis** is the one model call that leaves the premises:
  it runs on the controller's loopback provider listener and its prompt is C0
  telemetry only (rule/metric context + instance inventory, no workload payloads),
  per the user's decision of 2026-10-15 and
  [`docs/contracts/governance.md`](docs/contracts/governance.md) §7.1/§7.2 plus
  [`infra-decisions.md`](docs/contracts/infra-decisions.md) **D9**. The boundary
  hook `externalModelCall()` is enforced on that call and its audit row records
  `egress=external destination=aiplatform.googleapis.com`, so the egress is
  labelled rather than assumed away.
- **The controller is a GCE VM, not Cloud Run** — it persists to SQLite, and
  Cloud Run's filesystem is ephemeral and per-instance, which would break the
  single-owner operator lease and leave audit provenance unverifiable across
  restarts. Issue #96 (closed) records the reasoning and what a migration would
  require.

Compute Engine is still GCP, so the deployment requirement (Cloud Run / GCP /
Firebase) is met without a closed-model API.

Deploy scripts and the full runbook: [`infra/cloudrun/README.md`](infra/cloudrun/README.md).
The vCPU budget that constrains the fleet: [`infra/gcp/README.md`](infra/gcp/README.md).

---

## Verify

Acceptance commands, runnable from a fresh checkout. The repo-level CI sanity job
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the JSON/ULID/syntax
checks, the unit suite, and the README link check.

```bash
cd controller && make verify             # core acceptance: register→role, telemetry, 422-on-malformed
cd controller && make verify-operator    # operator loop vs the chaos harness (all scenarios)
./image/pipeline/verify-pipeline.sh      # image pipeline
node --test dashboard/tests/core.test.mjs   # dashboard unit tests
python3 w7/acceptance/run.py             # W1–W6 acceptance matrix
python3 -m unittest discover -s tests    # full unit + integration suite
```

Named suites and scripts: [controller/Makefile](controller/Makefile) (`make verify`
/ `make verify-operator`), [image/pipeline/verify-pipeline.sh](image/pipeline/verify-pipeline.sh),
[dashboard/tests/core.test.mjs](dashboard/tests/core.test.mjs),
[w7/acceptance/run.py](w7/acceptance/run.py).

---

## Governance boundary

The enforceable claim, not "zero data ever leaves": **no C2 customer data
egresses; only allowlisted C0 hardware/OS telemetry and transformed C1
identifiers leave, and only after a fail-closed controller-side governance gate**
([`docs/contracts/governance.md`](docs/contracts/governance.md) §3–§6). A
non-conforming batch is rejected whole (`422 GOVERNANCE_REJECT`) before any byte
is stored or forwarded.

Data classes ([`docs/contracts/governance.md`](docs/contracts/governance.md) §3):

- **C0** — hardware/OS telemetry. Allowed after allowlist + schema validation.
- **C1** — identifiers/metadata (hostname, IP, MAC, serials). Allowed only after
  transformation (hash/truncate/tokenize).
- **C2** — application logs, customer payloads, prompts, credentials. **Never**
  egresses; presence triggers fail-closed.

What Termini does **not** claim (the honest guardrails from
[`docs/narrative.md`](docs/narrative.md) §6):

- No "zero data ever leaves" — only the bounded C0/C1 claim above.
- No claim that the model's *choice* is reproducible. The deployed demo runs the
  decider **model-driven** (`TERMINI_OPERATOR_DECISION_MODE=model`): a threshold rule
  only *detects*; the model selects the action from an allowlist that
  grammar-constrained decoding makes physically unbreachable, with a deterministic
  provider as the mandatory fallback
  ([`docs/contracts/governance.md`](docs/contracts/governance.md) §7.3). What is
  guaranteed is the **safety envelope**, not the specific choice.
- No VM launch from natural language — the allocation path ends at a validated,
  clamped plan.
- No hotplug resize or node drain/migration in the **operator loop** — the
  operator restarts the runaway workload on OOM and stops the heaviest workload
  on disk failure. Node drain remains a *human- or assistant-initiated* action
  (the dashboard's drain control, or the assistant endpoint
  `controller-api.md` §4.10 on guardrail admission) — it is not an operator-loop
  action.
- No long-lived or shared SSH credentials — a controller-held SSH CA signs
  short-lived (≤8 h), principal-bound certificates.
- No on-node redactor that drops C2 fields — the gate is controller-side, and a
  non-conforming batch is rejected fail-closed, not field-dropped.
- No GPU-accelerated inference on the customer's node — the agent-facing model runs
  CPU-only `llama.cpp` on Node C; the diagnosis runs on a managed model service (Gemini
  on Vertex AI), which is an external call, not a GPU on the node.

---

## Contracts index

The eight frozen contract surfaces under [`docs/contracts/`](docs/contracts/), the
normative source of truth for anything crossing a component boundary
([`docs/architecture.md`](docs/architecture.md) §1):

1. [infra-decisions.md](docs/contracts/infra-decisions.md) — D1–D8 (LXD cluster,
   underlay network, images, SSH key, GPU, control channel, storage, telemetry)
2. [node-bootstrap.md](docs/contracts/node-bootstrap.md) — boot → register →
   LXD join → telemetry → SSH injection; failure/retry matrix
3. [controller-api.md](docs/contracts/controller-api.md) — REST/WS surface,
   tokens, error envelope, guardrail/clamp validator, command channel
4. [node-telemetry.md](docs/contracts/node-telemetry.md) — C0/C1 telemetry
   schema, ingest validation, threshold-trigger linkage
5. [dashboard.md](docs/contracts/dashboard.md) — dashboard ↔ controller
   (telemetry view, AI-operator log stream, terminal, manual intervention)
6. [vm-allocation.schema.json](docs/contracts/vm-allocation.schema.json) —
   `AllocationRequest` / `AllocationPlan` / guardrail-audit JSON Schema
7. [agent-runtime.md](docs/contracts/agent-runtime.md) — agent VM image, LXD
   launch contract, in-VM runtime + broker, llama.cpp inference interface
8. [governance.md](docs/contracts/governance.md) — C0/C1/C2 taxonomy, external-call
   boundary, audit chain, enforcement hooks

---

## Status & evidence

The W7 gate verdict — per-workstream results, blocking findings, and residual
risks — is recorded in [`w7/verdict.md`](w7/verdict.md), including the
independent data-egress (governance) verification. Read it for the authoritative
GO/NO-GO; this document does not assert one.

---

## Demo & judge materials

- [`SUBMISSION.md`](SUBMISSION.md) — **AI Builder Cup submission pack**: category
  specification, the mandatory-requirement → artifact map, live links, and the
  Google-AI/GCP evidence
- [`deck/`](deck/README.md) — the judge-facing slide deck (HTML + the PDF proposal)
- [`docs/video-script-3min.md`](docs/video-script-3min.md) — the 3-minute video
  script (required submission deliverable)
- [`docs/demo-script.md`](docs/demo-script.md) — the live 6-beat narration script
- [`docs/ui-demo-walkthrough.md`](docs/ui-demo-walkthrough.md) — per-element walkthrough
  of the Fleet destination: what each chart, indicator and control is and where it
  comes from
- [`docs/narrative.md`](docs/narrative.md) — positioning + the three proof points
- [`docs/spec-answers.md`](docs/spec-answers.md) — judge-facing answers to the
  four open spec questions
- [`docs/architecture.md`](docs/architecture.md) — the frozen architecture

---

## Contributing

Work lands through a PR, never a direct push to `main`. The agent working
agreement is [`.hermes.md`](.hermes.md): branch off `origin/main`, verify with the
acceptance commands above, commit explicit paths, push, open a PR, and land via
[`scripts/land.sh`](scripts/land.sh) (which refuses to merge a red PR).
Secrets and runtime state are never committed (see the pre-commit hook in
[`.githooks/`](.githooks/pre-commit)).

---

## Licence

No licence is present in this repository.
