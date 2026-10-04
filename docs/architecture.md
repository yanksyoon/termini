# Termini — System Architecture (W1)

- Owner: `architect` (kanban task `t_5fb81d14`)
- Status: **FROZEN** for the 5-day build. This document is the single entry point for every W1 workstream and the tie-breaker when two contract documents disagree.
- Date: 2026-10-04
- Audience: all W1 workstreams (controller, infra, agents, governance, frontend, verifier) and anyone onboarding to the repo.
- Requirements source: `termini/project.md`.

This document consolidates the five W1 contract surfaces and the W0 research survey into one
readable map. It is normative for anything that crosses a component boundary. Where a child
contract and this document disagree, **this document wins**; the child contract must be updated
to match (all such updates are applied in the same change set that added this file).

---

## 1. Document index (contracts)

| Area | Document | Owns |
|------|----------|------|
| Infra decisions | `docs/contracts/infra-decisions.md` | D1–D8 (LXD cluster, underlay network, images, SSH key, GPU, control channel, storage, telemetry) |
| Node bootstrap | `docs/contracts/node-bootstrap.md` | Node-side boot → register → LXD join → telemetry → SSH injection; failure/retry matrix |
| Controller API | `docs/contracts/controller-api.md` | REST/WS surface, tokens, error envelope, guardrail/clamp validator, command channel, event stream |
| Node telemetry | `docs/contracts/node-telemetry.md` | C0/C1 telemetry schema, ingest validation, threshold-trigger linkage |
| Dashboard | `docs/contracts/dashboard.md` | Customer dashboard ↔ controller: telemetry view, AI-operator log stream, terminal (WS + SSH string), manual intervention |
| Allocation schema | `docs/contracts/vm-allocation.schema.json` | Structural `AllocationRequest` / `AllocationPlan` / guardrail audit JSON Schema |
| Agent runtime | `docs/contracts/agent-runtime.md` | Agent VM image, LXD launch contract, in-VM runtime + broker, llama.cpp inference interface |
| Governance | `docs/contracts/governance.md` | C0/C1/C2 taxonomy, on-node redactor, external-call boundary, audit chain, enforcement hooks |
| W0 research | `docs/research/tech-landscape.md` | Build-vs-adopt verdicts, tool shortlist, BQ-1 model pin |

**Net moat (from `tech-landscape.md` §2):** adopt the substrate (LXD + Ubuntu), the connectivity
(underlay + controller-held SSH key), and the model (llama.cpp + GGUF Q4_K_M); build only two things — the
governance boundary and the deterministic AI-operator control loop.

---

## 2. Component diagram

```
                          ┌─────────────────────────────────────────────────────┐
                          │                 GCP Controller VM (10.10.0.10)       │
                          │                                                     │
   browser (project_token)│  ┌──────────┐   ┌──────────────────────────────┐    │
  ──── REST /v1 ─────────►│  │ REST API │──►│  guardrail / clamp validator  │    │
  ──── /stream/v1 ───────►│  │ /v1      │   │  (vm-allocation.schema.json)  │    │
  ──── /terminal/v1 ─────►│  └────┬─────┘   └──────────────┬───────────────┘    │
                          │       │ governanceGate          │                   │
                          │  ┌────▼─────────────────────────▼───────────────┐   │
                          │  │        AI operator loop (reconcile)           │   │
                          │  │  telemetry → threshold rule → playbook action │   │
                          │  │  → LXD REST (audited) → command/ack           │   │
                          │  └───────────────────────────────────────────────┘   │
                          └──────────────────────────────────────────────────────┘
                                                  │
            outbound-only mTLS                    │  WebSocket /node/v1/{id}/channel
                                                  │  heartbeat + telemetry + cmds + ack
  ┌───────────────────────────────▼─────────────────────────────────────────────┐
  │                          customer premises (underlay 10.10.0.0/24)           │
  │                                                                              │
  │  ┌───────────── Node A (seed) ─────────────┐   ┌──────── Node C (GPU) ──────┐ │
  │  │ 10.10.0.11   LXD member (seed) :8443    │   │ 10.10.0.13                │ │
  │  │ termini-bootstrap ──► termini-agent-    │   │ termini-infer-router :8080 │ │
  │  │                    │      broker :7071  │   │   ├ llama-cpp@flash :8081  │ │
  │  │                    ▼                     │   │   └ llama-cpp@pro   :8082  │ │
  │  │   agent VM (10.20.1.x) ──► 10.10.0.13:8080 (inference)                   │ │
  │  └──────────────────────────────────────────┘   └───────────────────────────┘ │
  │  ┌───────────── Node B (compute) ──────────┐   lxdbr0 10.20.2.0/24           │
  │  │ 10.10.0.12   LXD member :8443          │    (NAT to underlay; no overlay) │
  │  │ termini-bootstrap + broker + agent VMs  │                                  │
  │  └──────────────────────────────────────────┘                                 │
  └──────────────────────────────────────────────────────────────────────────────┘
```

Roles: Node A = LXD cluster seed + compute; Node B = compute; Node C = GPU node hosting the
host-exposed inference router (`:8080`). Agent VMs run only on A/B. There is **no LXD overlay
network** in the demo — cross-node traffic routes over the underlay (D2).

---

## 3. Deployment topology (frozen addressing)

| Scope | Value | Notes |
|-------|-------|-------|
| Underlay | `10.10.0.0/24` | GCP VPC subnet (demo) / customer LAN (on-prem) |
| Controller | `10.10.0.10:443` | Public HTTPS; also reachable from nodes as outbound target |
| Node A (seed) | `10.10.0.11:8443` | LXD cluster seed; controller addresses the cluster here |
| Node B | `10.10.0.12` | LXD cluster member |
| Node C (GPU) | `10.10.0.13:8080` | Host-exposed inference router (D5) |
| Guest bridges | `10.20.1.0/24`, `10.20.2.0/24`, `10.20.3.0/24` | Per-node NAT bridges (`lxdbr0`) |

Ports: `443` controller (public + node outbound); `8443` LXD API (peers + controller);
`8080` inference (underlay only); `22` SSH (admin CIDR, CA certs only). Full table: `infra-decisions.md` D2.

---

## 4. Sequence flows

### 4.1 Boot → register

1. Node boots the pre-baked host image; cloud-init writes `config.yaml`, CA pubkey, enrollment
   token; `systemd` starts `termini-bootstrap`.
2. Daemon syncs clock (chrony), gathers hardware facts, generates an Ed25519 keypair, sends a CSR.
3. `POST /v1/nodes/register` with `Authorization: Bearer <bootstrap_token>`.
4. Controller validates the single-use token, upserts by `machine_id`, assigns role
   (`compute | inference`), mints the client cert + node token + LXD join token.
5. Node persists identity, applies `lxd init --preseed` (seed first, then joiners), opens the
   mTLS control channel, and starts telemetry. Target **< 5 s** to registered (D3).

### 4.2 Deploy agent VMs

1. Frontend `POST /v1/projects/{id}/intents` (NL prompt or structured `spec`).
2. NL prompt → on-prem `deepseek-flash` extraction (`json_object` response format) → `AllocationRequest`.
3. Guardrail validator → `AllocationPlan` (all-or-nothing; clamp numeric, reject structural).
4. Frontend `POST /v1/projects/{id}/vms/apply` → controller accepts the plan, publishes
   `vm.provisioned` with `vm_ids: []`. There is **no `vm.launch` fan-out in the demo**:
   LXD materialization is the node command-channel / LXD task's scope and is not wired
   in the committed controller (`controller/app/server.py _apply_plan`).
5. (Future) node daemon calls its local LXD API (`POST /1.0/instances?target=<member>`),
   boots the preloaded `agent-vm/<version>` image, acks `running`. Target **< 10 s** to
   task-ready.

### 4.3 Telemetry

1. Node daemon pushes C0/C1 telemetry batches over the mTLS channel (demo 1 s, default 15 s).
2. On-node redactor gates every payload before the socket opens (fail-closed).
3. Controller `governanceGate` re-verifies; discrepancies → `422 GOVERNANCE_REJECT`.
4. Ingest into the ring-buffer TSDB (raw → 5 s/1 m/5 m downsample); feeds charts + the operator.

### 4.4 Remediation (AI operator reconcile loop)

1. Threshold rule fires on a telemetry sample (e.g. `oom-guard-01`).
2. The operator maps the rule to a fixed playbook action — `instance.restart` /
   `instance.stop` / `instance.delete` / `image.prune` — deterministically, with no
   model call in the loop.
3. The action executes against the node's LXD REST API; the executor reports the
   exact request it made (method/path/body + result).
4. Every autonomous change writes an audit record linking the telemetry trigger
   (`actor: ai_operator`, `trigger`, rule_id) to the action and result.
5. An **external model in the loop is day-2** (governance.md §7 governs the boundary
   if/when it is added); the demo loop is purely deterministic.

### 4.5 Inference (on-prem only)

1. `termini-agent` (in the VM) → on-node broker (`10.20.<n>.1:7071`) for tasks.
2. All LLM calls go to `http://10.10.0.13:8080` (OpenAI-compatible router) with a bearer token and
   `X-Termini-*` attribution headers.
3. Router routes the `model` alias explicitly (`deepseek-flash` resident / `deepseek-v4-pro` lazy);
   no silent substitution except opt-in `X-Termini-Allow-Degrade`.
4. Results (C2) stay on-prem; only the C0 result summary egresses via the node daemon.

---

## 5. Trust boundaries

| Boundary | Rule | Enforced by |
|----------|------|-------------|
| Agent VM → GCP | **No route.** Guest bridge cannot reach the controller's public IP. | Node firewall (D2) |
| Agent VM → inference | Bearer token + underlay-scoped `:8080` only. | Router auth + Node C firewall |
| Node → Controller (egress) | **C0/C1 hardware/OS telemetry only.** C2 never egresses; fail-closed. | On-node redactor + controller `governanceGate` |
| Controller → Node (inbound) | Typed commands only; task envelope may carry C2 **transiently** (never persisted/model-processed). | `commandAllowlist()` + inbound shape check |
| Controller → external model | **Not in the demo loop** (the operator is deterministic). Day-2 only: C0 telemetry + static versioned template. | `governance.md` §7 (future) |
| User SSH | Controller-held SSH key surfaced as a one-click web terminal + `ssh` string; the SSH CA + short-lived-cert model is day-2 (B7). | Controller-held key (D4) |

---

## 6. Failure modes (summary; full matrix in the child docs)

| Failure | Detection | Behavior |
|---------|-----------|----------|
| Controller unreachable | connect timeout | Backoff-retry forever; local services still start; no data egress (`node-bootstrap.md` §9) |
| Bad enrollment token | 401/403 | Terminal → `REGISTRATION_FAILED` (operator re-provisions) |
| LXD join mismatch | version/pool/network | Degrade, keep telemetry flowing; standalone fallback (D2) |
| Node disk degrading / read-only | `disk[].mount_ro` / `write_latency_ms` | `instance.stop` the heaviest workload + alert (Scenario B) |
| RAM exhaustion | `ram_used_bytes` ≥ 95% ×2 | `instance.restart` the heaviest workload (Scenario A) |
| GPU / inference down | `inference.server_up=false` ×3 | Alert operator (surfaced, no autonomous action); degrade to flash if opted in |
| Model unavailable | router probe | `503 MODEL_UNAVAILABLE` / `MODEL_NOT_FOUND`; never a wrong-but-plausible answer |
| Redactor crash / C2 detected | pipeline | Fail-closed: no socket, no egress (`governance.md` §6) |

Guiding rule everywhere: **a degraded node keeps reporting** so the operator can act, and
**autonomous actions are traceable to a telemetry trigger**.

---

## 7. Cross-component invariants

1. Identifiers (`node_id`, `project_id`, `vm_id`, `command_id`, `plan_id`, `task_id`, `call_id`,
   `audit_id`, `request_id`) are **ULIDs** (26-char Crockford base32, uppercase, no I/L/O/U).
2. Addresses/ports are static and frozen (D2 table); `INFERENCE_BASE_URL=http://10.10.0.13:8080`.
3. Only hardware/OS telemetry leaves a node, always through the bootstrap daemon.
4. No private keys transit the controller (user keys in browser; node keys on node; only CSR/public keys move).
5. LXD snap revision is identical across all members (mismatch ⇒ failed join ⇒ degraded).
6. Every cross-node service is reached by underlay IP:port, never a guest/overlay address.
7. No allocation reaches LXD without a passing guardrail validator run.
8. Model output never triggers an action directly — always clamp → allowlist → ack/audit.
9. Agent logs and task result bodies (C2) never egress; only C0/C1 aggregates do.

---

## 8. Reconciliation — final answers to every open question

This section records each contradiction between the child contracts, the final answer, and the
rationale. The child docs are updated in the same change set so no contradiction remains.

### R1 — Node ID format

**Answer:** `node_id` is a 26-char **ULID**, no prefix (e.g. `01M4349RPTT29RNTC6VBCXQ0RC`).
`node-bootstrap.md` used a `node_`-prefixed 14-char form (`node_01HZX8Y2K4Q7`); that is corrected.
**Rationale:** `controller-api.md` §1.3 and `governance.md` §4 already freeze ULIDs for every
identifier, and the allocation schema's `$defs.ulid` enforces the 26-char pattern.

### R2 — Registration authentication

**Answer:** `POST /v1/nodes/register` authenticates with `Authorization: Bearer <bootstrap_token>`
(the single-use, short-TTL enrollment token). The `X-Bootstrap-Token` header in `controller-api.md`
§4.1 is removed — it contradicted §2's "all tokens carried as `Authorization: Bearer`".
**Rationale:** one token carriage convention across the surface; `node-bootstrap.md` §5.2 and
`controller-api.md` §2 already agreed on Bearer.

### R3 — Registration wire contract

**Answer:** `node-bootstrap.md` §5.2/§5.3 is the canonical `NodeRegistration` request/response
(machine facts + `machine_id` + CSR + `host_ssh_keys` → `node_id` + role + client cert + cluster
plan + telemetry config). `controller-api.md` §4.1 is updated to the same body and additionally
returns `project_id` and `node_token` (for REST-fallback auth) and `registration_ttl_s`.
**Rationale:** the CSR, `machine_id`, and `host_ssh_keys` are required by D4/D6 (mTLS identity +
host-key pinning); the simplified body in `controller-api.md` could not satisfy them.

### R4 — Node control channel

**Answer:** the single outbound mTLS **WebSocket** at
`wss://<controller-host>/node/v1/{node_id}/channel` is the **primary** steady-state channel,
carrying heartbeat, telemetry, command delivery, and command acks (D6 "single connection").
The REST endpoints in `controller-api.md` §4.1 (`/heartbeat`, `/telemetry`, `/commands` long-poll,
`/commands/{command_id}/ack`) remain as an **HTTP fallback** for environments that block the WS
upgrade, and `/node/v1` joins `/stream/v1` and `/terminal/v1` as a WebSocket root outside `/v1`.
**Rationale:** resolves the three-way disagreement between D6 (single WS), `node-bootstrap.md`
(one `/channel`), and `controller-api.md` (REST-primary) without discarding any surface.

### R5 — Command surfaces (was "command allowlist")

**Answer:** the demo wires two command surfaces, neither of which includes `vm.launch`:

1. **AI-operator remediation** — the LXD REST actions the executor issues
   (`controller/app/operator/executor.py`): `instance.restart`, `instance.stop`,
   `instance.delete`, `image.prune`. These are the only autonomous actions.
2. **Human node actions** — `node.reboot` and `node.drain`
   (`POST /v1/projects/{pid}/nodes/{nid}/actions/{reboot,drain}`).

The broader `commandAllowlist()` set declared in `controller/app/commands.py`
(`vm.resize`, `vm.migrate`, `vm.stop`, `vm.start`, `vm.destroy`, `agent.dispatch_task`,
`agent.cancel_task`) is **declared but not wired to any execution path in the demo** — marked
future/day-2. `vm.launch` is **not** in the allowlist and there is **no launch path**
(`_apply_plan` returns `vm_ids: []`); VM materialization is the node command-channel / LXD
task's scope. **Rationale:** the doc now matches the committed controller; the `vm.*`/`agent.*`
verbs are honest about being future work, not shipped behavior.

### R6 — Error codes

**Answer:** the canonical error codes are the UPPER_SNAKE set in `controller-api.md` §3:
`INVALID_ARGUMENT · UNAUTHENTICATED · PERMISSION_DENIED · NOT_FOUND · CONFLICT ·
GUARDRAIL_REJECTED · GOVERNANCE_REJECT · UNSUPPORTED_VERSION · RATE_LIMITED · NODE_OFFLINE ·
INTERNAL`. `node-bootstrap.md` §5.4's lowercase codes map onto these (`invalid_request` →
`INVALID_ARGUMENT`, `token_invalid` → `UNAUTHENTICATED`, `token_scope_mismatch` →
`PERMISSION_DENIED`, `already_registered` → `CONFLICT`, `rate_limited` → `RATE_LIMITED`), and
`unsupported_version` is `426 UNSUPPORTED_VERSION` (not 422). **Rationale:** one error envelope
for the whole surface; the inference service (`agent-runtime.md`) is a separate surface with its
own OpenAI-style codes and is not affected.

### R7 — Reconnect endpoint

**Answer:** `POST /v1/nodes/{node_id}/reconnect` (node_token + mTLS) is added to
`controller-api.md` for idempotent re-enrollment after reboot/cert reissue
(`node-bootstrap.md` §10). **Rationale:** the endpoint existed only on the node side; the
controller must own it.

### R8 — Telemetry cadence

**Answer:** telemetry cadence **default 15,000 ms**, **demo 1,000 ms**; heartbeat **5 s**
(degraded 15 s, offline 45 s). The "every 2 s" figures in `controller-api.md` §8 and
`node-telemetry.md` §1 are corrected to 1 s demo / 15 s default, matching D8 and
`project.md` advisory #4 (1–2 s during the demo). **Rationale:** D8 is the frozen decision.

### R9 — C0 telemetry extensions (orphan registration)

**Answer:** the C0 extension groups `lxd_instances[]`, `inference{}`, `services[]`, and
`agents[]` are registered in `governance.md` §4.1 (same deny-by-default review as the core
allowlist). `node-telemetry.md` §4.9's interim "configure the redactor manually" note is retired.
**Rationale:** closes the flag from `t_260694d8` and the `agent-runtime.md` §17 item 3.

### R10 — G-AR invariants

**Answer:** `governance.md` §12 ratifies **G-AR-1** (node→controller never carries C2) and
**G-AR-2** (the controller carries task content only transiently — no persistence, no model
processing). **Rationale:** these are the load-bearing governance guarantees for task delivery.

### R11 — BQ-1: pin the model artifacts (open question, now answered)

**Answer:** the `deepseek-*` aliases are **distills**, not the V4 MoE family:

| Alias | Artifact | Quant | Weights | GPU budget |
|-------|----------|-------|---------|------------|
| `deepseek-flash` | DeepSeek-R1-Distill-Llama-8B | Q4_K_M | ~6 GiB | ~9 GiB (resident, ×4 parallel) |
| `deepseek-v4-pro` | DeepSeek-R1-Distill-Qwen-14B | Q4_K_M | ~18 GiB | ~22 GiB (lazy, ×1) |

Target GPU: **NVIDIA L4 24 GiB** (both fit). **T4 16 GiB** → `flash-only` (`deepseek-v4-pro`
unavailable, degrade path demonstrated). `governance.md` §7.1 wording is amended to state the
aliases are distills. **Rationale:** the literal V4 MoE (Flash 284 B ≈146 GiB, Pro 1.6 T) cannot
fit a single L4/T4; `agent-runtime.md` §11.1 already budgeted to these distill sizes. The exact
HF GGUF files are pinned by the Node C bake (infra) against these aliases.

### R12 — D5 / D7 wording

**Answer:** (a) `infra-decisions.md` D5 notes `:8080` is realized as an **OpenAI-compatible
router** fronting llama.cpp workers (single-model deployments collapse to passthrough) — port and
scope unchanged. (b) D7 wording is corrected: LXD local-pool **stateful** migration exists but
copies the disk; the demo's AI operator remediates via `instance.restart`/`instance.stop`
(`node-telemetry.md` §5), not resize/evacuation. **Rationale:** `agent-runtime.md` §17
item 5 and `tech-landscape.md` §5 item 3.

### R13 — `machine.provisioning` (W0 recommendation, non-blocking)

**Answer:** deferred to W2. `machine.image_version` already identifies the image; the
provisioning *method* (GCP machine image vs ipxe-netboot vs manual) is an on-prem onboarding
detail that does not change any W1 wire contract, and the delivery inputs are already covered by
`node-bootstrap.md` §3.2 (cloud-init `user-data` or GCP instance metadata). **Rationale:** no
W1 consumer blocks on this; recording it here closes the loop without expanding the register
schema now.

### R14 — Terminal target (W1-review A2)

**Answer:** the dashboard terminal is **node-level** (`/terminal/v1/nodes/{node_id}`, host
shell, controller-brokered over the outbound mTLS channel, D6), and a **VM-level** agent shell
lives at `/terminal/v1/vms/{vm_id}` (`lxc exec`). `controller-api.md` §7.3 and
`dashboard.md` §3.1 now agree. **Rationale:** the operator's primary terminal target is the
node host (break-glass/debug); the VM shell is a secondary surface for agent inspection.

### R15 — SSH user access (W1-review D1/A1)

**Answer:** user access is a **controller-held SSH key**, surfaced in the dashboard as a
one-click web terminal plus an `ssh` string (`GET /v1/projects/{pid}/nodes/{nid}/ssh`). No
passwords, no project private key shown in the UI. The **SSH CA + short-lived, principal-bound
certificate** model is the day-2/B7 hardening (tracked as controller task t_53df37ef), not the
demo path. The infra-injected break-glass key is a one-time recovery path **removed after
registration** (`node-bootstrap.md` §7). The `ssh.inject_key` command and the shared project
keypair are **removed everywhere**. **Rationale:** the demo shows real, honest access
(controller-held key + web terminal + `ssh` string); full CA-based revocation/rotation is a
future hardening, not a shipped claim.

### R16 — `inference_vm` is a reference, not a launchable VM (W1-review D2)

**Answer:** `inference_vm` is a **reference to the always-on, host-exposed llama.cpp service on
Node C** (D5), recorded and surfaced in the plan but NOT launched in the demo (zero VMs, zero GPU
allocation). `passthrough_gpu` is day-2 only: any `true` value is a structural rejection
(`gpu_passthrough_not_in_demo`), enforced by `clamp_validate` (`termini/intent/validator.py`) and
the schema. **B5 note (doc↔code):** the committed `_role_check` in `controller/app/server.py`
still requires `passthrough_gpu` for `inference_vm` (rejecting it with `gpu_required_for_inference`),
so an `inference_vm` allocation is rejected end-to-end until the controller B5 fix (t_53df37ef)
removes that rule and realizes `inference_vm` as `host_service` (no passthrough).
**Rationale:** the intent extractor must never emit a spec the demo guardrail cannot satisfy.

### R17 — Registration C1 bootstrap exception (W1-review D3)

**Answer:** the registration body carries `machine.hostname`, `machine.underlay_ip`, and
`machine.nics[].addresses` in the clear as a **bounded C1 bootstrap exception** (audited) —
the node's own addresses, required for LXD cluster join/routing. Raw MAC is removed from the
body (`nics[].mac` deleted); MAC never egresses except as the hashed C1 `mac_address`
telemetry field. **Rationale:** the node's underlay IP cannot be derived from the TLS
connection under NAT and is needed for cluster addressing; MAC is not needed at all.

### R18 — Example identifiers are 26-char ULIDs (W1-review D4)

**Answer:** every example identifier across the contract docs is a **26-char ULID**; the
30-char example strings (which failed schema self-validation) are regenerated. A CI check
(`scripts/check-doc-ulids.py`) rejects any 26+-char Crockford string in the spec docs.
**Rationale:** the schema pattern and `controller/app/ulid.py` already enforce 26 chars; only
the examples were wrong.

### R19 — Telemetry: single source of truth (W1-review N1/A3)

**Answer:** `node-telemetry.md` is the single source of truth; `docs/telemetry-schema.md` is
deleted and its enforcement-order content folded into `node-telemetry.md` §7. Three drifts are
reconciled: `governance_chain_tip` is **required**; `hostname` uses a project_id-derived salt;
controller validation is **fail-closed whole-batch** (any governance/schema violation rejects
the batch; per-sample `seq` dedup is reported in `{accepted, rejected}`).
`controller/app/fields.py` and `controller/scripts/acceptance.py` are updated to match.
**Rationale:** fail-closed whole-batch is the safer, simpler semantic at 1 s cadence.

### R20 — `labels` bounding (W1-review N4)

**Answer:** `labels` is a bounded C1 display-metadata map (≤16 keys, `^[a-z0-9][a-z0-9_-]{0,63}$`,
values ≤128 chars) and is excluded from `guardrail_audit.raw_spec` persistence (a SHA-256
summary is stored instead). **Rationale:** closes the free-text C2 vector into controller
storage (`governance.md` §10 rule 7).

### R21 — Project create/list endpoints (W1-review N5/A4)

**Answer:** `POST /v1/projects` (create → `project_id` + `project_token` + `bootstrap_token`)
and `GET /v1/projects` (list) are added to `controller-api.md` §4.2. **Rationale:** the
frontend cannot bootstrap a project or obtain a token without them.

### R22 — Nested→flat metric mapping (W1-review A6)

**Answer:** wire telemetry is **nested** (`disk[]`, `net[]`, `gpu[]`, `inference{}`); the 19
flat chart names are controller-derived with a frozen mapping (first element for the demo's
single disk/NIC/GPU, direct for flat host and `inference{}` fields), documented in
`dashboard.md` §1.4 and mirrored by `controller/app/store.py METRICS`. **Rationale:** one
unambiguous mapping for frontend/controller devs.

### R23 — Controller launch path (W1-review N2)

**Answer:** the controller does **not** drive LXD placement, and in the demo there is **no launch
path at all** — `_apply_plan` accepts the plan and returns `vm_ids: []`. The controller mints
join tokens at registration (seed `8443`, cluster admin only). VM placement via a typed
`vm.launch` command to the node daemon (which calls its **local** LXD API, AR-2) is the
day-2 / wired-out path. **Rationale:** matches D6's outbound-only topology and keeps exactly one
control path per node.

### R24 — OOM remediation determinism (W1-review N3)

**Answer:** `oom-guard-01` has a **deterministic local fast-path** (threshold →
`instance.restart` of the heaviest workload, no model call) so remediation is not gated on model
latency, plus a **deterministic chaos ramp** (the chaos harness drives the metric across the
threshold on cue) so the demo fires even when `stress-ng --vm-bytes 85%` never reaches 95%×2. The
kernel-OOM race is stated honestly as best-effort. **Rationale:** there is no model in the loop;
the demo must deterministically demonstrate a real remediation.

---

## 9. What "done" means (acceptance mapping)

- `docs/architecture.md` is the single entry point, linking every contract in §1.
- Every workstream can build against the contracts without asking: the component diagram,
  topology, sequence flows, trust boundaries, failure modes, and invariants (§2–§7) are complete.
- Every open spec question has a final answer with rationale (§8, R1–R24).
- No contradictions remain: `controller-api.md` ↔ `node-bootstrap.md` route naming and wire
  formats agree (R1–R8); the C0 extension groups and G-AR invariants are registered (R9–R10);
  BQ-1 is pinned (R11); D5/D7 wording reconciled (R12); no interface is referenced but
  undefined. The W1 adversarial-review defects are remediated (R14–R24): SSH access is the
  single D4 CA model, `inference_vm` is a reference to the host service (no passthrough), the
  registration C1 bootstrap exception is bounded (no raw MAC), all example IDs are 26-char
  ULIDs, `node-telemetry.md` is the sole telemetry schema, and the terminal/launch/metric
  surfaces have one authoritative answer each.
