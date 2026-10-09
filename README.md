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
day 0, a deterministic AI operator remediates it on day 2, and the whole loop is
gated by a fail-closed governance boundary that makes data residency *verifiable*.

---

## Architecture

Three diagrams, derived faithfully from the frozen
[`docs/architecture.md`](docs/architecture.md) §2 and §4. The normative detail —
frozen addressing, trust boundaries, failure modes, and the R1–R24 reconciliation
answers — lives there.

### 1. System overview

A GCP control plane (controller VM) and the customer premises (Nodes A/B compute +
Node C GPU inference) are joined by a single **outbound-only mTLS control
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
   │                 telemetry → threshold rule → playbook action │
   │                 → LXD REST (audited) → command / ack         │
   │                                                              │
   │        ▲ governance gate (fail-closed, C2 → 422 reject)      │
   └────────┼─────────────────────────────────────────────────────┘
            │  outbound-only mTLS control channel
            │  (heartbeat · telemetry · commands · acks)
   ┌────────┼─────────────────────────────────────────────────────┐
   │  customer premises — underlay 10.10.0.0/24                   │
   │                                                              │
   │  Node A (seed)      Node B (compute)     Node C (GPU)        │
   │  10.10.0.11         10.10.0.12          10.10.0.13 :8080     │
   │  LXD member         LXD member          llama.cpp router     │
   │  bootstrap          bootstrap           (on-prem inference)  │
   │  agent VMs          agent VMs                                 │
   └──────────────────────────────────────────────────────────────┘
```

Roles: Node A = LXD cluster seed + compute, Node B = compute, Node C = GPU node
hosting the host-exposed inference router (`:8080`). Agent VMs run only on A/B;
there is no LXD overlay in the demo — cross-node traffic routes over the underlay.

### 2. Day-0 bootstrap flow

Power-on to a registered, role-assigned, cluster-joined node in under 5 seconds
([`docs/architecture.md`](docs/architecture.md) §4.1).

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

Telemetry crosses a threshold, a deterministic rule selects a pre-approved
playbook action, and the remediation lands with a full audit trail — while a
non-conforming batch is rejected fail-closed at the governance gate
([`docs/architecture.md`](docs/architecture.md) §4.3–§4.4). **There is no model
in the loop** on this path.

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
   deterministic playbook action  (no model in the loop)
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

# 3. One fault scenario end to end (oom | disk-degrade | latency | healthy)
make chaos SCENARIO=oom
```

Details and environment knobs: [console/README.md](console/README.md),
[controller/README.md](controller/README.md), [chaos/README.md](chaos/README.md).

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
- No ML model proposing or selecting remediations in the live loop — the day-2
  operator is a deterministic threshold rule over frozen playbooks.
- No VM launch from natural language — the allocation path ends at a validated,
  clamped plan.
- No hotplug resize or node drain/migration — the operator restarts the runaway
  workload on OOM and stops the heaviest workload on disk failure.
- No long-lived or shared SSH credentials — a controller-held SSH CA signs
  short-lived (≤8 h), principal-bound certificates.
- No on-node redactor that drops C2 fields — the gate is controller-side, and a
  non-conforming batch is rejected fail-closed, not field-dropped.
- No GPU-accelerated inference in the live demo — it runs CPU-only `llama.cpp`.

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

- [`deck/`](deck/README.md) — the judge-facing HTML slide deck
- [`docs/demo-script.md`](docs/demo-script.md) — the live 6-beat narration script
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
