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
| Infra decisions | `docs/contracts/infra-decisions.md` | D1–D8 (LXD cluster, underlay network, images, SSH CA, GPU, control channel, storage, telemetry) |
| Node bootstrap | `docs/contracts/node-bootstrap.md` | Node-side boot → register → LXD join → telemetry → SSH injection; failure/retry matrix |
| Controller API | `docs/contracts/controller-api.md` | REST/WS surface, tokens, error envelope, guardrail/clamp validator, command channel, event stream |
| Node telemetry | `docs/contracts/node-telemetry.md` | C0/C1 telemetry schema, ingest validation, threshold-trigger linkage |
| Allocation schema | `docs/contracts/vm-allocation.schema.json` | Structural `AllocationRequest` / `AllocationPlan` / guardrail audit JSON Schema |
| Agent runtime | `docs/contracts/agent-runtime.md` | Agent VM image, LXD launch contract, in-VM runtime + broker, llama.cpp inference interface |
| Governance | `docs/contracts/governance.md` | C0/C1/C2 taxonomy, on-node redactor, external-call boundary, audit chain, enforcement hooks |
| W0 research | `docs/research/tech-landscape.md` | Build-vs-adopt verdicts, tool shortlist, BQ-1 model pin |

**Net moat (from `tech-landscape.md` §2):** adopt the substrate (LXD + Ubuntu), the connectivity
(underlay + SSH CA), and the model (llama.cpp + GGUF Q4_K_M); build only two things — the
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
                          │  │  telemetry → threshold rule → advisory model  │   │
                          │  │  → clamp → commandAllowlist() → command/ack   │   │
                          │  └────┬──────────────────────────────┬──────────┘   │
                          │       │ externalModelCall() (C0 only) │              │
                          └───────┼────────────────────────────────┼──────────────┘
                                  │                                │ external model (GCP, C0 telemetry only)
            outbound-only mTLS    │  WebSocket /node/v1/{id}/channel│  (advisory only)
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
2. NL prompt → on-prem `deepseek-flash` extraction (grammar-constrained JSON) → `AllocationRequest`.
3. Guardrail validator → `AllocationPlan` (all-or-nothing; clamp numeric, reject structural).
4. Frontend `POST /v1/projects/{id}/vms/apply` → controller fans out `vm.launch` commands to the
   resolved node's daemon over the command channel.
5. Node daemon calls the local LXD API (`POST /1.0/instances?target=<member>`), boots the
   preloaded `agent-vm/<version>` image, acks `running`. Target **< 10 s** to task-ready.

### 4.3 Telemetry

1. Node daemon pushes C0/C1 telemetry batches over the mTLS channel (demo 1 s, default 15 s).
2. On-node redactor gates every payload before the socket opens (fail-closed).
3. Controller `governanceGate` re-verifies; discrepancies → `422 GOVERNANCE_REJECT`.
4. Ingest into the ring-buffer TSDB (raw → 5 s/1 m/5 m downsample); feeds charts + the operator.

### 4.4 Remediation (AI operator reconcile loop)

1. Threshold rule fires on a telemetry sample (e.g. `oom-guard-01`).
2. Operator builds a **static, versioned** prompt over typed C0 fields only; `externalModelCall()`
   wraps the model call and asserts all-C0.
3. Model output is **advisory**: it passes `guardrailClamp()` then `commandAllowlist()`; a command
   is issued only if it survives both.
4. Node executes the typed command, acks `succeeded|failed` with a result object.
5. Every autonomous change writes a `guardrail_audit` record linking the telemetry trigger
   (`actor: ai_operator`, `trigger`, `command_id`).

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
| Controller → external model | C0 telemetry only, static versioned template, advisory output. | `externalModelCall()` |
| User SSH | CA-signed, short-lived (8 h), principal/source-bound certs; browser-local private keys. | Controller SSH CA (D4) |

---

## 6. Failure modes (summary; full matrix in the child docs)

| Failure | Detection | Behavior |
|---------|-----------|----------|
| Controller unreachable | connect timeout | Backoff-retry forever; local services still start; no data egress (`node-bootstrap.md` §9) |
| Bad enrollment token | 401/403 | Terminal → `REGISTRATION_FAILED` (operator re-provisions) |
| LXD join mismatch | version/pool/network | Degrade, keep telemetry flowing; standalone fallback (D2) |
| Node disk degrading / read-only | `disk[].mount_ro` / `write_latency_ms` | `node.drain` + alert (Scenario B) |
| RAM exhaustion | `ram_used_bytes` ≥ 95% ×2 | `vm.resize` or evacuate (Scenario A) |
| GPU / inference down | `inference.server_up=false` ×3 | Restart llama-cpp, alert; degrade to flash if opted in |
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

**Answer:** `node_id` is a 26-char **ULID**, no prefix (e.g. `01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C1E`).
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

### R5 — Command allowlist

**Answer:** the canonical command types (the `commandAllowlist()`) are:

`vm.launch · vm.resize · vm.migrate · vm.stop · vm.start · vm.destroy · node.drain ·
node.reboot · ssh.inject_key · agent.dispatch_task · agent.cancel_task`

`node-bootstrap.md` §11's illustrative verbs (`resize_instance`, `evacuate_instance`,
`set_telemetry_interval`, `issue_cert`, `run_healthcheck`) map to `vm.resize`, `node.drain`,
registration-response config (not a command), `ssh.inject_key`, and telemetry health (not a
command), respectively. **Rationale:** `vm.launch`/`agent.*` are required by
`agent-runtime.md` §17; `vm.*`/`node.*`/`ssh.*`/`agent.*` namespaces are unambiguous.

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
copies the disk; the demo still uses resize + evacuation. **Rationale:** `agent-runtime.md` §17
item 5 and `tech-landscape.md` §5 item 3.

### R13 — `machine.provisioning` (W0 recommendation, non-blocking)

**Answer:** deferred to W2. `machine.image_version` already identifies the image; the
provisioning *method* (GCP machine image vs ipxe-netboot vs manual) is an on-prem onboarding
detail that does not change any W1 wire contract, and the delivery inputs are already covered by
`node-bootstrap.md` §3.2 (cloud-init `user-data` or GCP instance metadata). **Rationale:** no
W1 consumer blocks on this; recording it here closes the loop without expanding the register
schema now.

---

## 9. What "done" means (acceptance mapping)

- `docs/architecture.md` is the single entry point, linking every contract in §1.
- Every workstream can build against the contracts without asking: the component diagram,
  topology, sequence flows, trust boundaries, failure modes, and invariants (§2–§7) are complete.
- Every open spec question has a final answer with rationale (§8, R1–R13).
- No contradictions remain: `controller-api.md` ↔ `node-bootstrap.md` route naming and wire
  formats agree (R1–R8); the C0 extension groups and G-AR invariants are registered (R9–R10);
  BQ-1 is pinned (R11); D5/D7 wording reconciled (R12); no interface is referenced but undefined.
