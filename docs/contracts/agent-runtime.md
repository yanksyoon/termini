# Termini — Agent VM & llama.cpp Inference Interface Contract (W1)

- Owner: `agents` (kanban task `t_1addb6fd`)
- Status: **FROZEN** for the 5-day build. Changes require a comment on the task and a matching update in `docs/architecture.md`.
- Date: 2026-10-04
- Audience: agent-VM authors, controller engineers, frontend, governance, verifier workstreams.
- Related: `docs/contracts/infra-decisions.md` (D1–D8), `docs/contracts/node-bootstrap.md`, `docs/contracts/controller-api.md`, `docs/contracts/node-telemetry.md`, `docs/contracts/vm-allocation.schema.json`, `docs/contracts/governance.md`, `docs/architecture.md`.
- Requirements source: `~/termini/project.md`.

**Scope of this document.** It freezes four interfaces other workstreams build against:

1. The **pre-baked agentic OS image** (`agent-vm`) — its contents, versioning, and launch-time configuration.
2. The **LXD/VM launch contract** the controller uses to materialize an `AllocationPlan` into agent VMs (and, day-2, inference workloads).
3. The **agent runtime** — how an agent inside a VM receives work, calls inference, emits logs, and exposes status without breaching the governance boundary.
4. The **on-prem llama.cpp GPU inference service** on Node C — endpoint, health, explicit model routing (alias **`gemma`**), request/response and streaming schema, GPU expectations, and failure/fallback rules.

Nothing here is a TBD. Where a value is not fixed below, the default in the referenced section applies.

**Clarification on the task wording "launch contract from node C".** Agent VMs are launched **on the compute nodes A/B**; Node C is the GPU node that **hosts inference**. The launch API is the **LXD cluster API addressed through the seed (Node A, `10.10.0.11:8443`)** with `target=<member>`, so Node C is a launch target only for the day-2 `inference_vm` class (D5 does not launch it in the demo). This document therefore specifies the launch contract for the whole cluster, and pins Node C's role as the inference host.

---

## 0. Decision summary (frozen)

| # | Decision | Rejected alternatives |
|---|----------|-----------------------|
| AR-1 | Immutable `agent-vm/<semver>` LXD VM image, preloaded on every host; contents fixed in §3. No installs/downloads at launch. | live `cloud-init` pip install, registry pull at launch |
| AR-2 | Launch is a typed `vm.launch` command to the node daemon, which calls the **local** LXD API; instance spec fixed in §4. | controller calling LXD REST on nodes directly (no node-side gate), free-form `lxc` argv |
| AR-3 | In-VM runtime `termini-agent` (systemd, unprivileged `agent`) + node-side `termini-agent-broker`. Fixed in §5–§6. | agents calling the controller directly, sidecar per agent |
| AR-4 | Inference is an **OpenAI-compatible router** on `http://10.10.0.13:8080` fronting one-or-more **llama.cpp workers** bound to loopback. | one monolithic llama.cpp on :8080 with no routing (cannot serve two aliases) |
| AR-5 | Model routing is **explicit**: request `model` selects the alias; no silent substitution except opted-in `X-Termini-Allow-Degrade`. §9. | implicit "pick a model" router, auto-upgrade/downgrade without consent |
| AR-6 | The agent resolves its inference model against the alias set the Node C endpoint actually serves, and refuses any alias outside it (`ModelNotAllowed`) — no silent substitution. Today that set is exactly **`gemma`** (Gemma 3 4B IT, resident; `image/models.lock.json` `shipped_resident_alias`). The image's catalog pins (`deepseek-flash`, `deepseek-v4-pro`) remain baked as documented reference distills and stay valid in telemetry, but they are **no longer the agent's wired aliases**. Residency policy §11. | more models, external model fallback |
| AR-7 | Deterministic failure/fallback matrix with named error codes; fail-closed to `503`, never silent partial answers. §12. | blind retries, silent model substitution |
| AR-8 | Agents receive work from an **on-node broker**; the controller delivers tasks over the node command channel. §7. | agents polling GCP, inbound push to agents |
| AR-9 | Agent application logs are **local-only**. Only C0 aggregates egress, via the node bootstrap daemon. §13. | forwarding raw agent logs to the controller |
| AR-10 | Controller↔agent surfaces: **intent extraction/execution** (on-prem flash) and **remediation commands**; fixed in §14. | agent talking to GCP APIs |
| AR-11 | Governance invariants: agent VM has **no route to GCP**; all agent egress is on-prem. §15. | permissive egress, "temporary" exceptions |
| AR-12 | Fixed verification commands. §18. | — |
| AR-13 | The agent runtime exposes a **task-submit surface inside the instance**, bound to loopback (`POST /v1/tasks` on the §5.1 listener); the controller reaches it **by proxying** through the owning node's LXD exec, never by handing the client a connect handle. §5.6/§7.4 (decision AT-1). | a client-reachable instance endpoint, per-instance credentials handed to the browser |
| AR-14 | An instance's task load is **bounded per instance** (`tasks.max_concurrent`, default 2): an over-bound submission is refused `429 TASK_BOUND_EXCEEDED`, never queued invisibly. §5.6. | unbounded per-instance concurrency |

These choices maximise demo reliability and keep the "your hardware, your data, our operations" claim enforceable: **the agent VM's only two network destinations are the on-prem broker and on-prem inference.**

---

## 1. Actors and responsibilities

| Component | Runs where | Responsibility |
|-----------|------------|----------------|
| Agent VM (`agent-vm/<ver>`) | LXD VM on Node A/B | Isolated execution environment for task-oriented agents. |
| `termini-agent` | Inside the agent VM (systemd) | Task loop, inference client, local structured logging, local status socket. |
| `termini-agent-broker` | Node host (companion to the bootstrap daemon) | Per-node task queue and result sink; the only path between controller and agent VMs. |
| `termini-bootstrap` daemon | Node host (systemd) | The **sole** node↔controller egress path (D6). Receives `vm.*`/`agent.*` commands, relays task envelopes to the broker, forwards C0 agent status in telemetry. |
| `termini-infer-router` | Node C host, `:8080` | OpenAI-compatible front door; routes aliases to llama.cpp workers; health; per-request accounting. |
| `llama-cpp@<alias>` | Node C host (systemd template) | One llama.cpp process per model alias, bound to loopback, full GPU offload. |
| LXD cluster `termini` (seed = Node A) | Nodes A/B/C | Hosts agent VMs; the launch API the controller drives. |
| GCP Controller | `10.10.0.10` | Plans, issues commands, ingests C0 telemetry, runs the AI operator loop, serves the frontend. |

---

## 2. Trust and data-flow boundaries (summary)

```
frontend ──(project_token)──► Controller (GCP 10.10.0.10)
                                 │  outbound-only mTLS (D6), typed commands
                                 ▼
   ┌──────────────── Node A/B host ────────────────┐        Node C host (GPU)
   │ termini-bootstrap ──► termini-agent-broker     │        ┌───────────────────┐
   │      │                     │  :7071 (lxdbr)     │  HTTP  │ termini-infer-    │
   │      │                     ▼                    │ ──────►│ router :8080      │
   │      │              agent VM (termini-agent) ───┼─8080──►│  ├ llama-cpp@flash│
   │      │  C0 telemetry only ▲                     │        │  └ llama-cpp@pro  │
   └──────┼─────────────────────┼───────────────────┘        └───────────────────┘
          └── node→controller egress is C0/C1 only ──► Controller
```

Invariants (restated as testable in §15):

- Agent VM → GCP: **no route** (node firewall blocks the guest bridge from reaching the controller's public IP).
- Agent VM → on-prem broker and on-prem inference only.
- Node → Controller egress carries **C0/C1 hardware/OS telemetry only** (governance §4/§6).
- Controller → Node (inbound to the premises) may carry a task envelope; the controller never persists or model-processes customer content.

---

## 3. Agent VM image (`agent-vm/<version>`)

### 3.1 Build inputs and versioning

- Built in CI (Packer + `virt-customize` on the Ubuntu 24.04 cloud image), exported as an LXD image tarball, **preloaded on every node's LXD image store** (D3). No registry pull at launch.
- **Alias:** `agent-vm/<semver>` (e.g. `agent-vm/2026.10.0`). **Immutable tag:** `agent-vm-<semver>+<gitsha>`; a content digest (`sha256:<hex>`) is recorded alongside.
- The running VM exposes its identity at `/etc/termini/agent-image`:
  ```
  agent-vm-2026.10.0+g1ab2c3d sha256:9f2c…
  ```
- **Skew policy:** the controller records each VM's image version in `GET /v1/projects/{id}/vms`. A VM more than **one minor version** behind the controller-expected alias is flagged `degraded` in the frontend; it is never auto-upgraded (upgrade = recreate with a new plan).

### 3.2 Contents (immutable; nothing installed at launch)

| Layer | Contents |
|-------|----------|
| Base | Ubuntu Server 24.04 LTS minimal, `systemd`, `chrony`, `nftables`, `openssh-server` (CA-only). |
| Runtime | Python **3.12** (`/opt/termini/venv`, wheels preloaded), Node.js **20 LTS** (`/opt/termini/node`). |
| Agent libs (Python, pinned in `/opt/termini/requirements.lock`) | `openai` (OpenAI-compatible client for the router), `httpx`, `pydantic`, `tenacity`, `structlog`, `langchain-core`, `langgraph` (optional, present for demo agents). |
| Agent libs (Node, `/opt/termini/package-lock.json`) | `openai`, `pino`, `undici`. |
| Termini runtime | `termini-agent` binary + `termini-agent.service` (enabled), unprivileged user `agent`, local status socket `/run/termini/agent.sock`. |
| Config templates | `/etc/termini/agent.yaml` (rendered by cloud-init at launch, §4.4), `/etc/termini/inference.token` (0600, `agent:agent`). |
| Logging | Local JSONL sink `/var/log/termini/agent.jsonl` with `logrotate`; nothing ships off-box. |
| Explicitly **absent** | model weights, cloud credentials, GCP endpoints, SSH private keys, the inference *router* itself. |

### 3.3 Runtime security posture

- `termini-agent` runs as `agent` with **no sudo**; it cannot touch LXD, the node firewall, or host devices.
- The VM has a single NIC on the node's `lxdbr0` guest bridge (D2); its only reachable peers are `10.20.<n>.1` (host) and the underlay services firewalled open to guests (Node C `:8080`). There is no NAT rule that lets a guest reach the controller's public IP (§15).
- Inference credentials are per-project scoped and to the on-prem endpoint only.

---

## 4. LXD/VM launch contract

### 4.1 Trigger path

`POST /v1/projects/{project_id}/vms/apply` (controller-api §4.2) accepts a `plan_id`. For each accepted allocation the controller issues a typed command to the resolved node's daemon over the mTLS command channel (controller-api §6):

```json
{
  "command_id": "01M4349RPY26W8Q453PZYK05CB",
  "type": "vm.launch",
  "payload": {
    "vm_id": "01M4349RPXWDC52ZDAVMPCMJSH",
    "project_id": "01M4349RPSZFTB60AVJEG0ZZY9",
    "class": "agent_vm",
    "image": "agent-vm/2026.10.0",
    "member": "node-a",
    "limits": { "vcpus": 2, "ram_gb": 4, "disk_gb": 8 },
    "network": { "name": "lxdbr0" },
    "profile": "termini-agent",
    "cloud_init": { "project_id": "01M4349RPSZFTB60AVJEG0ZZY9",
                    "inference_default_model": "gemma" },
    "idempotency_key": "01M4349RPVPP8X0W99R3TNZXW7"
  },
  "issued_by": "system",
  "issued_at": "2026-10-04T07:49:00Z",
  "timeout_s": 120
}
```

- `vm.launch` is a **proposed addition** to `commandAllowlist()` (controller-api §6) alongside `vm.destroy`; free-form `lxc` argv is never accepted. The node daemon validates the payload against this contract before executing.
- The node daemon **calls the local LXD API** (`POST /1.0/instances?target=<member>` on the seed, or the member's own API) — the controller never speaks LXD directly to a node (AR-2).
- `idempotency_key` (or the command's `Idempotency-Key`) makes a redelivered launch a no-op if an instance with that `vm_id` already exists.

### 4.2 Instance specification

| Attribute | Value |
|-----------|-------|
| Instance name | `agent-<last 8 chars of vm_id>` (the ULID `vm_id` is authoritative; the name carries no customer data) |
| Type | `virtual-machine` (QEMU; VM-grade isolation per project.md) |
| Source | preloaded alias `agent-vm/<version>` |
| Target member | resolved node (`node-a` / `node-b`); `inference_vm` targets `node-c` (day-2 only) |
| Profile | `termini-agent` (base: `limits.cpu`, `limits.memory`, root disk, `eth0`) |
| Config | `limits.cpu = vcpus`, `limits.memory = ram_gb GiB`, root disk `disk_gb GiB` (all already guardrail-clamped, controller-api §5) |
| Devices | `root` → pool `local` (zfs, D7); `eth0` → network `lxdbr0` |
| Boot | cloud-init `user-data` (§4.4) writes config and starts `termini-agent.service`; target **< 10 s to task-ready** (no installs/downloads) |

Equivalent CLI (the daemon's behavior, for verification):

```bash
lxc launch agent-vm/2026.10.0 agent-01H4KZ9Q \
  --vm --target node-a \
  --profile termini-agent \
  -c limits.cpu=2 -c limits.memory=4GiB \
  -d root,size=8GiB \
  --config cloud-init.user-data="$(cat /var/lib/termini/agent-user-data.yaml)"
```

Config applied through the LXD API (JSON), the canonical form:

```json
{
  "name": "agent-01H4KZ9Q",
  "type": "virtual-machine",
  "source": { "type": "image", "alias": "agent-vm/2026.10.0" },
  "target": "node-a",
  "profiles": ["termini-agent"],
  "config": {
    "limits.cpu": "2",
    "limits.memory": "4GiB",
    "cloud-init.user-data": "#cloud-config\nwrite_files:\n  - path: /etc/termini/agent.yaml\n    permissions: '0600'\n    content: |\n      project_id: 01M4349RPSZFTB60AVJEG0ZZY9\n      vm_id: 01M4349RPXWDC52ZDAVMPCMJSH\n      node_id: 01M4349RPTT29RNTC6VBCXQ0RC\n      inference:\n        base_url: http://10.10.0.13:8080\n        default_model: gemma\n        allowed_models: [gemma]\n        token_file: /etc/termini/inference.token\n        stream: true\n        connect_timeout_s: 2\n        request_timeout_s: 120\n      broker:\n        endpoint: http://10.20.1.1:7071\n        pull_interval_s: 1\n        max_concurrent_tasks: 2\n      logging:\n        sink: /var/log/termini/agent.jsonl\n        local_only: true\n      governance:\n        telemetry_class: C0\n        egress: deny\nruncmd:\n  - [ systemctl, enable, --now, termini-agent.service ]\n"
  },
  "devices": {
    "root": { "type": "disk", "pool": "local", "path": "/", "size": "8GiB" },
    "eth0": { "type": "nic", "network": "lxdbr0" }
  }
}
```

### 4.3 Lifecycle and idempotency

| Transition | Command | Behavior |
|-----------|---------|----------|
| create | `vm.launch` | Launch per §4.2; ack `running` + `{ip, image_version}`. |
| resize | `vm.resize` | `limits.cpu`/`limits.memory` hotplug where the guest supports it; else flag `reboot_required`. |
| stop | `vm.stop` | Graceful: `SIGTERM` to `termini-agent`, drain in-flight task (§5.4), then stop. |
| start | `vm.start` | Resume; `termini-agent` reloads checkpointed state. |
| destroy | `vm.destroy` | Stop, then delete; broker purges that VM's queue. |
| migrate | `vm.migrate` | Demo uses **stop→transfer→start** (evacuation) because storage is per-member local (D7); `live:true` is rejected with `409 CONFLICT` while `D7` holds. |
| drain | `node.drain` | Evacuate all agent VMs off the node (Scenario B). |

Every ack follows `POST /v1/nodes/{node_id}/commands/{command_id}/ack` (controller-api §6) with `status ∈ acked|running|succeeded|failed` and a `result` object (`vm.launch` returns `{vm_id, ip, image_version, member}`).

### 4.4 Launch-time config contract (`/etc/termini/agent.yaml`)

Rendered by cloud-init; the **only** inputs the agent runtime reads at start. Schema is stable; unknown keys are ignored (forward-compatible).

| Key | Required | Type | Meaning |
|-----|----------|------|---------|
| `project_id` | yes | ULID | Scoping / telemetry. |
| `vm_id` | yes | ULID | Identity presented to the broker and inference router. |
| `node_id` | yes | ULID | Owning node. |
| `inference.base_url` | yes | URL | `http://10.10.0.13:8080` (frozen; D5). |
| `inference.default_model` | yes | enum | `gemma` (default; the alias the Node C endpoint serves). |
| `inference.allowed_models` | yes | array | `[gemma]`; the runtime refuses any other alias (`ModelNotAllowed`). |
| `inference.token_file` | yes | path | Per-project scoped bearer for the router. |
| `inference.stream` | no | bool | Default `true`. |
| `inference.connect_timeout_s` | no | int | Default `2`. |
| `inference.request_timeout_s` | no | int | Default `120`; first-token deadline is 30 s. |
| `broker.endpoint` | yes | URL | `http://10.20.<n>.1:7071` (node's guest-bridge gateway). |
| `broker.pull_interval_s` | no | int | Default `1`. |
| `broker.max_concurrent_tasks` | no | int | Default `2`; ≤ the VM's vCPU count. |
| `tasks.max_concurrent` | no | int | Default `2`; the **per-instance task bound** of §5.6. An image/config predating this key falls back to `broker.max_concurrent_tasks`, never to unlimited. |
| `tasks.result_sink` | no | path | Default `/var/lib/termini/agent/results`; where the C2 answer body is kept on the premises (§7.3). |
| `logging.sink` | yes | path | Local JSONL (never egressed). |
| `governance.telemetry_class` | yes | enum | `C0` (the only class an agent may contribute to egress). |
| `governance.egress` | yes | enum | `deny` — the VM has no controller route. |

---

## 5. In-VM agent runtime (`termini-agent`)

### 5.1 Process model

- `termini-agent.service` (Python entry `/opt/termini/venv/bin/termini-agent`) starts at boot, reads `/etc/termini/agent.yaml`, and opens:
  - the **local status socket** `/run/termini/agent.sock` (unix, `agent:agent`), and
  - a **localhost-only** status mirror at `127.0.0.1:9101/status` for in-VM tooling.
- It runs up to `broker.max_concurrent_tasks` task workers, each with its own inference client.

### 5.2 Task loop

```
poll broker (/v1/tasks/claim) ──► for each task:
  1. validate envelope (task_id, kind, ttl_s)
  2. select model: task.model_preference → allowed alias, else inference.default_model
  3. run agent steps; every LLM call goes to inference.base_url only
  4. emit structured local log lines (C0 fields + task_id; NEVER raw content to logs at C1+)
  5. write result to the broker result sink (C2 stays on-prem, §7.3)
  6. ack task state to broker
```

### 5.3 Local state

- In-flight task state is checkpointed to `/var/lib/termini/agent/tasks/<task_id>.json` (0600) so a resize/stop/restart resumes rather than loses work.
- On boot, `termini-agent` re-claims checkpointed tasks that are still within `ttl_s`; a task past its TTL is acked `failed` with `reason: task_expired`.

### 5.4 Drain semantics (for remediation)

On `SIGTERM` (from `vm.stop`, `vm.resize`, `node.drain`, or node shutdown): stop claiming new tasks, wait up to **20 s** for in-flight tasks to finish or checkpoint, then exit 0. A task that cannot finish is checkpointed and re-queued by the broker (`state: requeued`). The VM never drops a task silently.

### 5.5 Inference client rules

- Base URL is **hard-coded from config**; the client refuses any other host (no env override in production images).
- Requests carry `X-Termini-Request-Id` (ULID), `X-Termini-Vm-Id`, `X-Termini-Project-Id`, and `Authorization: Bearer <token>`.
- Streaming is default; the client handles SSE `[DONE]`, keep-alive comments, and mid-stream errors (§10.4).
- Retry policy: only idempotent-safe failures (`connect timeout`, `503 INFERENCE_BUSY` with `Retry-After`) are retried, ≤ 2 times with jitter; a first-token timeout is retried once, then the task fails with `INFERENCE_TIMEOUT`.

### 5.6 In-VM task surface (`POST /v1/tasks`) — card `t_1a2c913c`

The runtime exposes a **task-submit surface inside the instance**, on the same
loopback listener as `/status` (`127.0.0.1:9101`, §5.1). It is the interface a
client's task becomes once it has crossed the node boundary; §7.4 defines how the
controller reaches it. Being bound to loopback is the security property: the
surface has no network reachability of its own, so the only way in is code
execution inside the instance — which only the owning node's control plane has.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/tasks` | Submit one task. Body `{ "prompt": str \| "messages": [{role,content}], "model_preference": str?, "max_tokens": int?, "temperature": float?, "actor": str? }`. `202` + the task record (`state: "queued"`), or the §7.4 refusal codes. |
| `GET`  | `/v1/tasks/{task_id}` | The task's current state **and, when terminal, its answer** (`answer`, `model`, `provenance`, `usage`, `duration_ms`). `404 TASK_NOT_FOUND` for an unknown id. |
| `GET`  | `/v1/tasks?limit=N` | The most recent N task summaries (C0 fields only — no prompts, no answers). |
| `GET`  | `/status` | Unchanged C0 status snapshot (§13.2), which now also carries the `tasks{}` counters below. |

**States** (§7.4): `queued → running → done | error`, and terminal means terminal —
`done` requires non-empty content from the configured endpoint; every other outcome
is `error` with a distinct code.

**Bounds (frozen):**

| Bound | Value | Meaning |
|-------|-------|---------|
| `tasks.max_concurrent` | default **2** (falls back to `broker.max_concurrent_tasks` on an image predating the key) | Tasks this **instance** runs at once. A submission while the bound is reached is refused immediately — `429 TASK_BOUND_EXCEEDED` + `Retry-After` — never queued invisibly. This is what stops a client's task load saturating the node. |
| prompt size | **8192 bytes** | `413 PROMPT_TOO_LARGE`. |
| `max_tokens` | 1..**4096** | `INVALID_ARGUMENT` outside the range. |
| task result retention | **64** records per instance | Older records are dropped from the local listing; the result body stays at the on-prem sink. |

**Refusals and failures are codes, never silence** (the §10.5 family):

| Code | When | Caller action |
|------|------|---------------|
| `TASK_BOUND_EXCEEDED` | the per-instance bound is reached | retry after `Retry-After` (or use another instance) |
| `MODEL_NOT_ALLOWED` | `model_preference` (or the configured default) is not in `inference.allowed_models` — e.g. a retired catalog alias | fix the request or the config; the runtime never substitutes |
| `MODEL_NOT_FOUND` / `MODEL_UNAVAILABLE` | the endpoint does not serve the alias / has no usable backend | check the endpoint wiring |
| `INFERENCE_UNREACHABLE` | the endpoint is not answering at all (connect refused/timeout on connect) | check node C; the agent stays healthy |
| `INFERENCE_TIMEOUT` | the endpoint accepted the call but exceeded `inference.request_timeout_s` | retry, or lower `max_tokens` |
| `EMPTY_COMPLETION` | HTTP 200 with empty content | treat as a failure — never as an empty answer |
| `CONTEXT_LENGTH_EXCEEDED`, `INFERENCE_BUSY`, `INFERENCE_ERROR` | §10.5 mappings | as §12 |

`INFERENCE_UNREACHABLE` is deliberately distinct from `INFERENCE_TIMEOUT`: "the
endpoint is down" and "the model is slow" are different operational facts, and a
task must never be reported as finished because a slow call was cut short.

**Provenance is carried on the result, never assumed.** Every answer records what
actually served it, taken from the endpoint's own in-band labels (`termini_backend`
block / `X-Termini-*` headers, `infra/inference/`): `backend`, `answered_by`,
`egress`, `destination`, `forwarded`. A forwarded managed model therefore cannot be
rendered as local inference, and an endpoint that publishes no label is reported as
`backend: "unknown"` — never as "local".

**Content handling.** The answer body is the customer's content (C2): it is written
to the on-prem sink `/var/lib/termini/agent/results/<task_id>.json` (0600) and
returned on this localhost surface only. Log lines carry the C0 counters — task id,
state, model, tokens, backend, duration — never a prompt or an answer (§13.1).

---

## 6. Node-side agent broker (`termini-agent-broker`)

- Shipped with the host image, run under the `termini` user as `termini-agent-broker.service`; a companion to the bootstrap daemon (they may share a process in the demo).
- Listens on the node's **guest-bridge gateway** `10.20.<n>.1:7071` (reachable only from that node's guest subnet) and on `127.0.0.1:7071`.
- It is the **only** path that moves tasks/results between the controller and agent VMs. The controller never addresses a VM directly.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/tasks/claim` | Agent claims up to `max` queued tasks for its `vm_id`. Body: `{"vm_id": ULID, "token": "...", "max": 2}`. |
| `POST` | `/v1/tasks/{task_id}/ack` | Agent reports `running \| succeeded \| failed \| requeued` + a C0 **result summary** (§6.2). |
| `POST` | `/v1/tasks/{task_id}/result` | Stores the C2 result body to the **on-prem result sink** (not egressed). |
| `GET`  | `/v1/status` | Local health/queue depth (consumed by the bootstrap daemon for C0 telemetry). |

### 6.1 Authentication

- At launch the broker mints a per-VM opaque token and injects it into the VM (`/etc/termini/agent.token`) via cloud-init; the agent presents `vm_id` + token. Cross-VM access is impossible (token↔vm_id binding).
- Broker→controller and controller→broker traffic is **not** direct: the broker exchanges with the bootstrap daemon over loopback, and the daemon uses the existing mTLS channel (D6). This keeps exactly one egress/ingress socket per node.

### 6.2 Result summary (C0 only — the only part the controller sees)

```json
{
  "task_id": "01M4349RPZ4DMD3MT18EBZRQB4",
  "vm_id": "01M4349RPXWDC52ZDAVMPCMJSH",
  "state": "succeeded",
  "started_at": "2026-10-04T07:49:02Z",
  "ended_at": "2026-10-04T07:49:41Z",
  "duration_ms": 39012,
  "steps": 6,
  "model_calls": [ { "model": "gemma", "tokens_in": 812, "tokens_out": 340, "degraded": false } ],
  "error_code": null
}
```

No field here may carry customer content (governance §4). The C2 result body lives only at the on-prem result sink and is surfaced to the customer through their own access path (brokered terminal or the customer-side store), never via controller storage.

---

## 7. Work delivery and results

### 7.1 Direction and governance

Two channels, deliberately asymmetric:

- **Controller → node (inbound to the premises):** `agent.dispatch_task` / `agent.cancel_task` commands carry the task envelope, which **may contain C2** (the customer submitted it to the platform). The controller relays it **transiently**: no persistence, no logging of content, no external model call (governance §7). The node's `governanceGate` equivalent on inbound is a **shape check only** (identity, size cap 256 KiB, `kind` enum) — C2 inbound is allowed because it is *entering* the customer's premises.
- **Node → controller (egress from the premises):** C0/C1 only. The **result summary** (§6.2) and telemetry (§13) may egress; the **result body** and any agent payload may not.

> Invariant **G-AR-1**: the node→controller direction never carries C2. Invariant **G-AR-2**: the controller never persists or model-processes task content. (Flagged for ratification in `governance.md` §12; see §17.)

### 7.2 Task envelope (controller → node → broker → agent)

```json
{
  "task_id": "01M4349RPZ4DMD3MT18EBZRQB4",
  "project_id": "01M4349RPSZFTB60AVJEG0ZZY9",
  "vm_id": "01M4349RPXWDC52ZDAVMPCMJSH",
  "kind": "agent.run",
  "created_at": "2026-10-04T07:48:59Z",
  "ttl_s": 900,
  "payload": {
    "instruction": "<customer text; C2 — on-prem only>",
    "context_refs": [],
    "model_preference": "auto"
  },
  "trace_id": "01M4349RPWA213SX85ZABPJWCH"
}
```

`kind` ∈ `agent.run | agent.control` (only `agent.control`, carrying no C2, is permitted to be issued autonomously by the AI operator). `model_preference` ∈ `auto | gemma`; `auto` = the runtime's default alias. A `model_preference` naming a retired alias (a catalog distill) is refused with `ModelNotAllowed` — it is not silently substituted.

Delivery is at-least-once; `task_id` is the dedup key. The broker keeps a bounded per-VM queue (default 64 tasks) and applies backpressure (`429` on claim overflow).

### 7.3 Results

- The result body (C2) is written to the on-prem result sink `/var/lib/termini/agent/results/<task_id>.json` on the node and is retrievable by the customer's own session. It is never included in any controller payload.
- The controller learns only the C0 summary (§6.2) and emits `remediation.completed`/task status events to the frontend (controller-api §7.1) — content-free.

### 7.4 Task submission through the controller — decision **AT-1** (card `t_1a2c913c`)

The client-facing route (shapes in `controller-api.md` §4.8):

```
POST /v1/projects/{project_id}/agents/{vm_id}/tasks      → 202 + the task record
GET  /v1/projects/{project_id}/agents/{vm_id}/tasks/{task_id}
GET  /v1/projects/{project_id}/agents/{vm_id}/tasks
```

`vm_id` **is** the agent's LXD instance name (§4.2; `controller-api.md` §5). The
controller resolves it against the live allocation records first and then against
the registered nodes' LXD instance lists, so an instance that was materialised by an
earlier controller process (or adopted on the node) is still addressable. It is not
reachable at all → `404 AGENT_NOT_FOUND` (with the known ids); the owning node is
registered but unreachable → `503 NODE_UNREACHABLE`; the node is offline →
`503 NODE_OFFLINE`.

**Decision AT-1: the controller PROXIES the call; it does not return a connect
handle.** The transport is the controller's own authenticated path to the resolved
node's LXD REST API (the same path `controller-api.md` §5/§6 already uses for
`instance.*` and materialisation), running one `exec` **inside the instance** to
speak to the loopback surface of §5.6. The client never learns an instance address
and never holds instance credentials.

| Option | Security consequence |
|--------|---------------------|
| **Proxy (chosen)** | Task content (C2) transits the controller in both directions, exactly as §7.1 already requires for the inbound direction: relayed transiently, **never** written to a log line, **never** sent to an external model by the controller. The cost is that a controller compromise exposes task content in flight; the benefit is that nothing about the instance is exposed and the client needs no route into the guest network. |
| Connect handle (rejected) | Would require the client (a browser on the public internet) to reach the instance's guest-bridge address — in this deployment there is **no route** from outside the node to `10.174.247.0/24`, so the surface would have to be bound to the guest bridge, and a per-instance credential would have to be handed to the browser. Strictly larger attack surface for no capability gain. |

**Record and audit.** Every submission is audited exactly like a node action: an
`agent.dispatch_task` command in the frozen envelope/allowlist (`commands.py`,
`issued_by: human`, node-scoped) plus a content-free operator-log line, and an
enumerable `agent_tasks` row (`controller/app/migrations/0015_agent_tasks.sql`).
The row is the client's read path for its own task (`GET .../tasks/{task_id}`); it
carries the prompt and answer **only** because the project-token holder is the
documented reader — the controller never logs either, and G-AR-2's operational
content ("no logging, no external model call, no persistence outside the project's
own record") is what the implementation enforces (see §17 item 6).

**Progress, deadlines and failure modes.** The controller-side deadline for one
task is **300 s** (`DEFAULT_TASK_TIMEOUT_S`), polled every **2 s** against the
agent's `GET /v1/tasks/{id}`; the states the client sees are the runtime's own
(`queued | running | done | error`), so a UI renders the same story the instance
knows. A task that outlives the deadline becomes `error: TASK_TIMEOUT`; a
controller restart marks any inherited non-terminal row `error: TASK_ORPHANED`.
Neither is ever reported as a slow-but-fine task, and neither is a success.

| Distinct failure the client sees | Meaning |
|----------------------------------|---------|
| `404 AGENT_NOT_FOUND` | no such instance on the project's nodes (details carry the known ids) |
| `503 NODE_OFFLINE` / `503 NODE_UNREACHABLE` | the owning node is offline / cannot be reached — the task is *not* dispatched (nothing was sent) |
| `503 AGENT_UNREACHABLE` | the instance answered the exec but its runtime has no task surface (an image predating §5.6), or the exec itself failed |
| `429 TASK_BOUND_EXCEEDED` | the agent refused: its per-instance bound (§5.6) is in flight; `Retry-After` is returned |
| `400 MODEL_NOT_ALLOWED` | the agent refused the alias (a retired catalog alias, AR-6) |
| `413 PROMPT_TOO_LARGE` | prompt over 8192 bytes (refused by the controller and the agent) |
| task `error` states | recorded on the task row with the runtime's code (`INFERENCE_UNREACHABLE`, `INFERENCE_TIMEOUT`, `EMPTY_COMPLETION`, `TASK_TIMEOUT`, `TASK_ORPHANED`, …) |

Transport-level refusals are also recorded on the task row before the API error is
returned, so a failed submission is as auditable as a successful one.

---

## 8. Inference service on Node C

### 8.1 Topology

```
agent VM (10.20.1.x) ─┐
controller intent-extract ─┐
local op tooling ─────┴──► termini-infer-router  :8080  (0.0.0.0, on Node C underlay IP)
                                   │ loopback only
                     ┌─────────────┴───────────────┐
                     ▼                             ▼
          llama-cpp@deepseek-flash        llama-cpp@deepseek-v4-pro
          127.0.0.1:8081                  127.0.0.1:8082
          (resident by default)           (lazy; see §11)
```

- The router realises D5's "host-exposed llama.cpp, OpenAI-compatible API on `:8080`". A single-model deployment (only `deepseek-flash` wired) collapses the router to a transparent passthrough.
- Firewall (D2): `allow tcp/8080 from 10.10.0.0/24` only; workers bind to loopback and are unreachable off-host.
- Workers run as `llama-cpp@<alias>.service` (systemd template), `--n-gpu-layers -1`, `--host 127.0.0.1 --port 808<k>`, `--flash-attn`, model/ctx from §11.

### 8.2 Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/health` | none | Liveness; `200` while the router process is up. |
| `GET` | `/health?model=<alias>` | token | Readiness for a specific alias (`resident`/`loading`/`ready`). |
| `GET` | `/v1/models` | token | Lists wired aliases with residency state. |
| `POST` | `/v1/chat/completions` | token | Chat completion (streaming and non-streaming). |
| `GET` | `/props` | token | Router/worker build + context params (diagnostics). |
| `GET` | `/metrics` | token, loopback-or-underlay | Prometheus text: queue depth, tok/s, VRAM, per-alias counters (feeds telemetry §4.7). |

`Authorization: Bearer <inference_token>`; missing/invalid → `401` with the OpenAI-style error envelope (§10.5). The token is per-project, scoped to `:8080`, and distributed only to that project's agent VMs and to the controller's intent-extraction caller.

### 8.3 Health semantics

- **Liveness** — `/health` returns `200 {"status":"ok"}` iff the router can accept connections. It does **not** depend on workers.
- **Readiness** — `/health?model=<alias>`:
  - `200 {"status":"ready","model":"gemma","residency":"resident"}` — serving now;
  - `503 {"status":"unavailable","model":"…","reason":"worker_failed|gpu_unavailable|not_wired"}` — cannot serve. A retired catalog alias (`deepseek-flash`, `deepseek-v4-pro`) answers `not_wired`; no lazy alias is wired today, so there is no `residency":"lazy"` case.
- The router is **not** considered started until `gemma` is resident (systemd `ExecStartPost` gate). A worker crash flips that alias to `unavailable` and raises telemetry `inference.server_up=false` → trigger `gpu-oos-01` (node-telemetry §5).

---

## 9. Model routing (explicit)

The wired alias set today is exactly **`{gemma}`** (AR-6): the shipped single-model deployment on Node C. The request `model` field **always** selects the alias; the two-alias rows below document the router's *capability* and are NOT wired in this deployment. Examples elsewhere in this document that name a catalog distill illustrate the wire format, not the current wiring.

| Alias | Workload | Default residency | Max parallel | Default context | Notes |
|-------|----------|-------------------|--------------|-----------------|-------|
| `gemma` | **Wired today (AR-6)** — Gemma 3 4B IT, Q4_K_M, `models.lock.json` `shipped_resident_alias`; serves the operator's decision step and the agents | resident | 4 | 16384 | The shipped model; a single-model deployment collapses the router to a passthrough. |
| `deepseek-flash` | Catalog pin — **not wired** (AR-6); kept in the image and legal in telemetry | resident (catalog) | 4 | 8192 | Retired as an agent/operator alias; a request for it is `404 MODEL_NOT_FOUND`. |
| `deepseek-v4-pro` | Catalog pin — **not wired** (AR-6); kept in the image and legal in telemetry | lazy (catalog) | 1 | 16384 (max 32768 if VRAM allows) | Retired as an agent/operator alias; a request for it is `404 MODEL_NOT_FOUND`. |

Routing rules:

1. `model` not in the wired set (today: anything but `gemma`) → `404 MODEL_NOT_FOUND` (no implicit routing). A catalog distill is refused here rather than substituted.
2. `model` wired but its worker unavailable → `503 MODEL_UNAVAILABLE` (§12). **The cross-alias degrade path is INERT today**: it needs two wired aliases and only one is wired, so a degrade-enabled request has no target and is refused like any other — stated here rather than implied, because "there is a fallback" would otherwise be false. Trade this amendment makes: reproducibility is kept, the second alias is surrendered.
3. `auto` is a **client-side** convenience only: `termini-agent` resolves `auto` → `inference.default_model` (= `gemma`); no long-reasoning escalation alias is wired today. The router never receives `auto`; if it does, it is `400 INVALID_ARGUMENT`.
4. Exactly one alias serves a given request; no fan-out, no ensembling, no external model (governance §7).

---

## 10. Request/response and streaming schema

The wire format is the OpenAI Chat Completions shape plus Termini **headers** (so existing OpenAI clients work unchanged).

### 10.1 Request

```json
{
  "model": "gemma",
  "messages": [
    { "role": "system", "content": "You are a Termini ops agent." },
    { "role": "user", "content": "Summarize this host's health." }
  ],
  "stream": true,
  "max_tokens": 512,
  "temperature": 0.2,
  "top_p": 0.95,
  "stop": ["\n\n"],
  "seed": 7
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `model` | yes | the wired alias (`gemma`), or a catalog alias, which is refused (§9). |
| `messages` | yes | 1..64 items; `role ∈ system|user|assistant`; `content` is a string. |
| `stream` | no | default `false`. |
| `max_tokens` | no | default 1024; clamped to the alias context budget. |
| `temperature` | no | default 0.2; range 0–2. |
| `top_p` | no | default 0.95. |
| `stop` | no | ≤ 4 strings. |
| `seed` | no | integer; deterministic sampling where the backend supports it. |
| `tools` | no | passthrough only; if present the router returns `400 INVALID_ARGUMENT` (tools require an alias that supports them, and none is wired today) (§9). |

**Request headers**

| Header | Required | Meaning |
|--------|----------|---------|
| `Authorization` | yes | `Bearer <inference_token>`. |
| `Content-Type` | yes | `application/json`. |
| `X-Termini-Request-Id` | yes | ULID; echoed for tracing. |
| `X-Termini-Vm-Id` / `X-Termini-Project-Id` | yes (VM callers) | Attribution for accounting/telemetry. |
| `X-Termini-Priority` | no | `interactive` (default) \| `batch`. |
| `X-Termini-Allow-Degrade` | no | `true` to permit pro→flash substitution (§9.2). |

### 10.2 Non-streaming response

```json
{
  "id": "chatcmpl-01M4349RQ09HMAX6RC9YPYQH4W",
  "object": "chat.completion",
  "created": 1791101000,
  "model": "gemma",
  "choices": [
    { "index": 0, "message": { "role": "assistant", "content": "Host healthy: CPU 31%, RAM 57%, GPU 88%." },
      "finish_reason": "stop" }
  ],
  "usage": { "prompt_tokens": 42, "completion_tokens": 31, "total_tokens": 73 }
}
```

**Response headers:** `X-Termini-Request-Id` (echo), `X-Termini-Model` (alias actually served), `X-Termini-Residency`, `X-Termini-Queue-Ms`, `X-Termini-Degraded`.

### 10.3 Streaming response

`Content-Type: text/event-stream`. One `data:` line per chunk, terminated by `data: [DONE]`.

```
data: {"id":"chatcmpl-01H4KZ…","object":"chat.completion.chunk","created":1791101000,"model":"gemma","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-01H4KZ…","object":"chat.completion.chunk","created":1791101000,"model":"gemma","choices":[{"index":0,"delta":{"content":"Host "},"finish_reason":null}]}

data: {"id":"chatcmpl-01H4KZ…","object":"chat.completion.chunk","created":1791101000,"model":"gemma","choices":[{"index":0,"delta":{"content":"healthy"},"finish_reason":null}]}

data: {"id":"chatcmpl-01H4KZ…","object":"chat.completion.chunk","created":1791101000,"model":"gemma","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":42,"completion_tokens":31,"total_tokens":73}}

data: [DONE]
```

### 10.4 Streaming rules

- The router emits SSE **keep-alive comments** (`: keep-alive`) every **15 s** while awaiting the first token.
- **First-token deadline: 30 s.** If exceeded, the router emits one error chunk (§10.5) with `code: INFERENCE_TIMEOUT` and closes the stream.
- **Max stream duration: 300 s**; on exceed → error chunk `INFERENCE_TIMEOUT`, close.
- Client disconnect cancels generation and frees the slot immediately.
- `usage` is emitted in the final chunk when the request sets `stream_options.include_usage: true`.
- No binary frames; UTF-8 text only.

### 10.5 Error envelope

```json
{
  "error": {
    "message": "model gemma is not resident and could not be loaded",
    "type": "termini_error",
    "code": "MODEL_UNAVAILABLE",
    "param": "model",
    "request_id": "01M4349RQ09HMAX6RC9YPYQH4W"
  }
}
```

| HTTP | `code` | Meaning |
|------|--------|---------|
| 400 | `INVALID_ARGUMENT` | Malformed body, `model: auto`, tools on flash, params out of range. |
| 401 | `UNAUTHENTICATED` | Missing/invalid inference token. |
| 404 | `MODEL_NOT_FOUND` | `model` not in the wired set. |
| 413 | `CONTEXT_LENGTH_EXCEEDED` | Prompt + `max_tokens` exceeds the alias context. |
| 429 | `INFERENCE_BUSY` | Queue full / no free slot; includes `Retry-After`. |
| 503 | `MODEL_UNAVAILABLE` | Worker down/unrecoverable or GPU unavailable; includes `Retry-After`. |
| 504 | `INFERENCE_TIMEOUT` | First-token or total-stream deadline exceeded. |
| 500 | `INFERENCE_ERROR` | Any other backend error; includes a backend `detail` (sanitized, no prompt content). |

Error bodies never contain prompt text or customer content.

---

## 11. GPU resource expectations and residency

### 11.1 Budget model

VRAM required by an alias ≈ `weights + KV_cache(ctx, parallel)` + `~1 GiB` overhead. Per-alias demo config:

| Alias | Quant | Weights (approx) | KV @ default ctx | Default total budget | Max parallel |
|-------|-------|------------------|------------------|----------------------|--------------|
| `gemma` | Q4_K_M | ~2.32 GiB | ~2 GiB | **~5 GiB** | 4 |
| `deepseek-flash` (catalog) | Q4_K_M | ~6 GiB | ~2.5 GiB | **~9 GiB** | 4 |
| `deepseek-v4-pro` | Q4_K_M | ~18 GiB | ~3 GiB | **~22 GiB** | 1 |

(Target demo GPU: NVIDIA **L4 24 GiB**; T4 16 GiB is supported with `deepseek-v4-pro` unavailable — see §12.)

### 11.2 Residency policy (metric-driven, observable)

Let `T = gpu_vram_total_bytes` on Node C (from telemetry, node C only).

**Wiring note (AR-6).** Exactly one alias is wired today (`gemma`, resident); the policies below describe the router's *capability* and are not exercised until a second alias is wired.

- **`dual`** (`T ≥ flash_budget + pro_budget + 1 GiB`): both resident, no swaps.
- **`single-heavy`** (otherwise): `deepseek-flash` resident at rest; a `deepseek-v4-pro` request triggers **lazy load** — the router drains flash requests, unloads `llama-cpp@deepseek-flash`, loads `llama-cpp@deepseek-v4-pro`, serves, and after `pro_idle_ttl_s` (default **300 s**) of no pro traffic unloads pro and restores flash.
- **`flash-only`** (`T < pro_budget`): `deepseek-v4-pro` reports `503 MODEL_UNAVAILABLE reason: gpu_insufficient`; only degrade-enabled requests are served (as flash).

Lazy-load and eviction are **reported**: telemetry carries `inference.model`, `inference.queue_depth`, `inference.active_requests`, and the router's `X-Termini-Residency` header reflects `loading` transitions via `429 INFERENCE_BUSY` + `Retry-After` for concurrent callers.

### 11.3 Concurrency and fairness

- Per-alias slots: flash 4, pro 1 (GPU is serialised for pro). Excess requests wait up to **`queue_timeout_s` = 10 s**, then `429 INFERENCE_BUSY`.
- `X-Termini-Priority: interactive` (agent VMs) preempts `batch` (background jobs) at the queue.
- CPU/temperature guards: if `gpu_temp_celsius ≥ 88` or `gpu_vram_used_bytes` pressure triggers the worker's OOM guard, the router sheds `batch` first, then returns `503 MODEL_UNAVAILABLE` rather than crashing.

### 11.4 Baseline expectations (acceptance-relevant)

- `deepseek-flash` cold start ≤ **15 s**; `deepseek-v4-pro` lazy load ≤ **60 s** (demo L4, pre-warmed weights on local disk).
- Steady-state throughput target ≥ **30 tok/s** for flash, ≥ **10 tok/s** for pro (single stream) on an L4.
- GPU utilisation and VRAM are visible via `gpu_util_pct` / `gpu_vram_used_bytes` (node-telemetry §4.5) and `inference.vram_used_bytes` (§4.7).

---

## 12. Failure and fallback matrix

Guiding rules: **never return a wrong-but-plausible answer**; **degrade visibly**; **the telemetry stream is authoritative** for what the operator sees.

| # | Failure | Detected by | Router behavior | Caller (agent/runtime) behavior |
|---|---------|-------------|-----------------|--------------------------------|
| 1 | `deepseek-v4-pro` worker down | router probe | `503 MODEL_UNAVAILABLE` + `Retry-After: 5`; trigger `gpu-oos-01` telemetry | retry ≤ 2 if degrade allowed; else fail task `INFERENCE_UNAVAILABLE` |
| 2 | GPU unavailable / driver lost | telemetry `inference.server_up=false` | all aliases `503 gpu_unavailable` | task fails fast; agent stays `idle`; no busy-loop |
| 3 | GPU VRAM insufficient for pro | startup/`/health?model` | `503 gpu_insufficient` | degrade to flash if opted in, else fail |
| 4 | Queue full (`INFERENCE_BUSY`) | router | `429` + `Retry-After` | backoff-retry ≤ 2 with jitter, then `failed` |
| 5 | First-token timeout | router | one error chunk `INFERENCE_TIMEOUT`, close | retry once, then fail `INFERENCE_TIMEOUT` |
| 6 | Context over budget | router | `413 CONTEXT_LENGTH_EXCEEDED` | shrink `max_tokens`/messages once, then fail `INVALID_ARGUMENT` |
| 7 | Mid-stream worker crash | router | error chunk `INFERENCE_ERROR`, close; restart worker | the partial output is **not** accepted; task retried once |
| 8 | Router process down | node telemetry `inference.server_up=false`; systemd restart | — | task fails fast; VM remains healthy and retries on next poll |
| 9 | Node C unreachable from agent VM | client connect timeout (2 s) | — | retry ≤ 2; if persistent, task `failed: inference_unreachable`; surfaced as a node health event |
| 10 | Inference token invalid | `401` | — | auth error propagates; task fails; controller flags project misconfig |
| 11 | Upstream (both aliases) unavailable | router | `503` | no silent substitution; task fails; controller raises an incident |

**Never**: silently substitute a model, return a truncated answer as complete, or retry non-idempotent work without a checkpoint.

---

## 13. Logs and status to the controller (governance-safe)

### 13.1 Agent logs — local only

- `termini-agent` emits structured **JSONL** to `/var/log/termini/agent.jsonl` (rotated, local). Each line is `{"ts","level","vm_id","task_id","event","…"}`.
- **Application/customer content is never written to a log line at C1 or above** and never egresses. If a log line would carry content, the runtime logs only `content_len` (C0).
- The controller's `log.line` / `GET /v1/projects/{id}/logs` channel (controller-api §7.2) carries **AI-operator operational messages only** — never agent or application logs (governance §7).

### 13.2 Status — the only agent-derived egress

Agent status reaches the controller **only** as C0 counters folded into node telemetry by the bootstrap daemon. Proposed extension block `agents[]` (parallel to `lxd_instances[]` in node-telemetry §4.6):

```json
{
  "agents": [
    { "vm_id": "01M4349RPXWDC52ZDAVMPCMJSH",
      "state": "busy",
      "current_task_id": "01M4349RPZ4DMD3MT18EBZRQB4",
      "queue_depth": 0,
      "tasks_completed": 12,
      "tasks_failed": 1,
      "model_in_use": "gemma",
      "last_task_at": "2026-10-04T07:49:41Z" }
  ]
}
```

| Field | C-class | Type | Notes |
|-------|---------|------|-------|
| `vm_id` | C0 | ULID | Controller-issued. |
| `state` | C0 | enum | `starting|idle|busy|error|stopped`. |
| `current_task_id` | C0 | ULID\|null | Non-content. |
| `queue_depth` | C0 | int | ≥ 0. |
| `tasks_completed` / `tasks_failed` | C0 | int | Monotonic counters. |
| `model_in_use` | C0 | enum\|null | `gemma` (served alias; catalog pins remain legal, matching `node-telemetry.md` §4.7). |
| `last_task_at` | C0 | RFC3339\|null | Timestamp. |

The same snapshot (and `/status`) carries a content-free `tasks{}` block for the §5.6 surface: `bound`, `in_flight`, `queued`, `completed`, `failed`, `tasks_completed_total`, `tasks_failed_total`, `last_task_id`. It is counters only — never a prompt or an answer.

No field carries content. This block is an **extension registration** item (see §17).

### 13.3 What the frontend sees

- Node/GPU/inference charts from the existing telemetry fields (`inference.*`, `gpu[]`, `lxd_instances[]`, and the new `agents[]`).
- Agent **counters and states** (no content). Task **content** is only visible to the customer through their own on-prem access path.

---

## 14. Controller interfaces: remediation and intent execution

### 14.1 Intent extraction (on-prem, DeepSeek flash)

The allocation flow (`controller-api.md` §4.2) routes a natural-language prompt to **on-prem** inference, never to an external model (governance §7). The controller calls:

```
POST http://10.10.0.13:8080/v1/chat/completions
Authorization: Bearer <inference_token>
X-Termini-Request-Id: <ULID>
```

with `model: "gemma"`, a **static, versioned extraction template**, and a **grammar/JSON-schema constraint** so output parses directly as an `AllocationRequest` (`vm-allocation.schema.json`). The prompt and the model output are on-prem; only the resulting structured JSON (counts, vcpus, ram_gb, target_node — non-sensitive) proceeds to the guardrail validator.

```json
{
  "model": "gemma",
  "messages": [
    { "role": "system", "content": "Extract an AllocationRequest JSON. Schema: {...}. Output JSON only." },
    { "role": "user", "content": "<customer prompt; C2; on-prem only>" }
  ],
  "temperature": 0,
  "max_tokens": 512,
  "response_format": { "type": "json_object" }
}
```

> `response_format` with `json_object` (or a GBNF grammar) is required so extraction is deterministic and never free-form. If extraction fails to produce schema-valid JSON, the controller returns `422 GUARDRAIL_REJECTED` and asks the user to restate — it never guesses.

### 14.2 Intent execution (plan → VMs)

1. `POST /v1/projects/{id}/intents` → `AllocationPlan` (guardrail-validated; controller-api §5).
2. `POST /v1/projects/{id}/vms/apply` → controller fans out typed commands over the node command channel:
   - `agent_vm` → `vm.launch` (§4) to the resolved compute node; image `agent-vm/<version>`.
   - `inference_vm` → **not launched in the demo** (D5). The Node C inference service is pre-baked and always-on; the plan's `inference_vm` class is recorded and surfaced but realized as the host service. Day-2 maps it to `vm.launch` on Node C with the `inference/<version>` image.
3. Progress is surfaced on the WS event stream (`vm.provisioned`, `vm.state_changed`, controller-api §7.1).

### 14.3 Remediation commands the agent runtime must honor

| Command (controller-api §6) | Agent-runtime behavior |
|-----------------------------|------------------------|
| `vm.resize` | Guardrail-clamped limits applied; drain or hotplug per §4.3; checkpoint in-flight tasks. |
| `vm.stop` | Drain (§5.4), checkpoint, exit 0. |
| `vm.start` | Re-claim checkpointed tasks within TTL. |
| `vm.destroy` | Final drain; broker purges the VM queue. |
| `vm.migrate` | Evacuation (stop→transfer→start, D7); VM resumes on the target with the same `vm_id`. |
| `node.drain` | Evacuate every agent VM off the node (Scenario B); tasks requeued on the target. |
| `node.reboot` | Drain then host reboot; identity survives (node-bootstrap §10). |
| `agent.dispatch_task` / `agent.cancel_task` | Enqueue/cancel a task envelope (§7.2). |

Every remediation that changes allocation also writes a `guardrail_audit` record with `actor: ai_operator` + the telemetry trigger (controller-api §5.4, node-telemetry §5), so autonomous changes are traceable.

### 14.4 Task-relevant events on the stream

Additive to controller-api §7.1 (proposed): `task.queued`, `task.state_changed` (`{task_id, vm_id, state}`), `task.completed` (`{task_id, vm_id, duration_ms, model}`). All content-free (C0/C1).

---

## 15. Governance boundaries and scoping (invariants, testable)

1. **No GCP route from an agent VM.** The node firewall drops guest-bridge → controller-public-IP; only the host daemon egresses. Test: from the VM, `curl` to the controller fails; `curl http://10.10.0.13:8080/health` succeeds.
2. **Inference is scoped.** `tcp/8080` on Node C is allowed only from `10.10.0.0/24` (D2); workers bind loopback; a bearer token is required. Test: from off-underlay/without token → refused.
3. **Agent logs never egress.** No code path sends `/var/log/termini/agent.jsonl` off-box; the controller log channel is AI-operator-only.
4. **Only C0/C1 agent status egresses** and only via the node daemon's allowlisted telemetry (§13.2).
5. **No customer content reaches an external model.** All LLM calls from agents and from intent extraction go to `10.10.0.13:8080`; `externalModelCall()` (governance §9) is never invoked with task content.
6. **Every inference call is attributable** via `X-Termini-Request-Id` + `X-Termini-Vm-Id`, and countable via `/metrics` + `inference.*` telemetry.
7. **No silent model substitution.** Routing is explicit (§9); the only substitution is opt-in and visibly marked (`X-Termini-Degraded: true`).

---

## 16. Demo simplifications (explicit, honest)

- A single per-project static inference token (rotated with the project) rather than per-VM short-lived tokens. Scope is limited to the on-prem `:8080` endpoint, so blast radius is a single on-prem service.
- The broker and bootstrap daemon may run in one process for the demo; the interface (§6) is unchanged.
- `deepseek-v4-pro` may be backed by a smaller quant on a T4; if it does not fit, §11.2 `flash-only` applies and the degrade path is demonstrated instead.
- Live migration is not offered (`D7`); `vm.migrate` is evacuation.

---

## 17. Cross-document reconciliation items (for the architect, `t_5fb81d14`)

These are the only places this contract extends a sibling document. **All five are now
landed** in the integration pass (t_5fb81d14, `docs/architecture.md` §8):

1. **`controller-api.md` §6** — `vm.launch`, `agent.dispatch_task`, `agent.cancel_task` added to the node command envelope / `commandAllowlist()`. ✅
2. **`controller-api.md` §7.1** — `task.queued` / `task.state_changed` / `task.completed` events added. ✅
3. **`node-telemetry.md` §4.6 / `governance.md` §4.1.1** — the `agents[]` C0 extension (§13.2) registered in the egress allowlist (same treatment as `lxd_instances[]`/`inference`/`services`). ✅
4. **`governance.md` §12** — invariant **G-AR-1/G-AR-2** ratified: the node→controller direction never carries C2; the controller carries task content only transiently (no persistence, no external model call). ✅
5. **`infra-decisions.md` D5** — `:8080` realized as an OpenAI-compatible **router** fronting llama.cpp workers (single-model deployments collapse to passthrough); endpoint, port, and scope unchanged. ✅
6. **`controller-api.md` §4.8 / §7.1 / §3** — the three agent-task routes, the `task.*` events and the task error codes, added by `t_1a2c913c` in the same PR as this section. ✅ The one place this card NARROWS an invariant: **G-AR-2** ("the controller never persists task content") is implemented as "the controller never logs task content, never hands it to an external model, and keeps it only in the project's own task record, which is the project-token holder's documented read path". Flagged for ratification in `governance.md` §12 — the alternative (no record at all) leaves the client unable to read back its own answer, which is the capability the card exists to deliver.

The node redactor treats the `agents[]` keys as C0 allowlist entries per the registered
group in `governance.md` §4.1.1.

---

## 18. Verification (acceptance test)

All commands run **from inside an agent VM** unless noted.

1. **Image/runtime up:**
   ```bash
   systemctl is-active termini-agent                 # active
   cat /etc/termini/agent-image                      # agent-vm-2026.10.0+g1ab2c3d sha256:…
   curl -sS http://127.0.0.1:9101/status | jq .state # "idle"
   ```
2. **Inference reachable + models listed:**
   ```bash
   curl -sS http://10.10.0.13:8080/health | jq .
   curl -sS -H "Authorization: Bearer $TOK" http://10.10.0.13:8080/v1/models | jq '.data[].id'
   # -> "gemma"
   ```
3. **Non-streaming chat:**
   ```bash
   curl -sS -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
     -H "X-Termini-Request-Id: 01M4349RQ09HMAX6RC9YPYQH4W" -H "X-Termini-Vm-Id: $VMID" \
     -d '{"model":"gemma","messages":[{"role":"user","content":"ping"}],"max_tokens":8}' \
     http://10.10.0.13:8080/v1/chat/completions | jq '.choices[0].message.content'
   ```
4. **Streaming (SSE):**
   ```bash
   curl -N -sS -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
     -d '{"model":"gemma","messages":[{"role":"user","content":"count to 5"}],"stream":true}' \
     http://10.10.0.13:8080/v1/chat/completions | tail -2
   # last two events: a finish chunk, then "data: [DONE]"
   ```
5. **Explicit routing + no silent substitution:**
   ```bash
   # unknown model -> 404 MODEL_NOT_FOUND
   curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOK" \
     -d '{"model":"gpt-4","messages":[{"role":"user","content":"x"}]}' \
     http://10.10.0.13:8080/v1/chat/completions
   ```
6. **Governance: no route to the controller (must fail):**
   ```bash
   curl --max-time 3 -sS "$TERMINI_CONTROLLER_URL/health" ; echo "exit=$?"   # non-zero / timeout
   ```
7. **On the node (operator):** telemetry shows the C0 blocks feeding the operator:
   ```bash
   journalctl -u termini-agent-broker -n 20
   # controller: GET /v1/projects/{id}/nodes/{node_id} -> metrics_latest.inference.server_up == true,
   #             metrics_latest.agents[] states, gpu[0].util_pct
   ```
8. **Task API (§5.6/§7.4), from the client side:** submit a task to a named agent, follow it to a terminal state, and read the answer with its provenance:
   ```bash
   TOKEN=<project_token>; PID=<project_id>; VM=termini-<plan>-0-0
   curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"prompt":"Reply with exactly: pong","actor":"human:ops"}' \
     "$CTRL/v1/projects/$PID/agents/$VM/tasks" | tee /tmp/task.json    # 202, state=queued
   TID=$(jq -r .task_id /tmp/task.json)
   curl -sS -H "Authorization: Bearer $TOKEN" \
     "$CTRL/v1/projects/$PID/agents/$VM/tasks/$TID" | jq '{state,answer,model,provenance,error_code}'
   curl -sS -H "Authorization: Bearer $TOKEN" \
     "$CTRL/v1/projects/$PID/agents/$VM/tasks" | jq '.count, .tasks[0].state'
   ```
   A refusal must be distinct and actionable — never a timeout that reads like    success: `429 TASK_BOUND_EXCEEDED` (two tasks in flight), `400 MODEL_NOT_ALLOWED`    (`model_preference: deepseek-flash`), `404 AGENT_NOT_FOUND` (unknown `vm_id`),    `503 AGENT_UNREACHABLE` (agent runtime without §5.6), and a task `error` with    `INFERENCE_UNREACHABLE` when the endpoint itself is down.

**Acceptance** holds when: an agent VM boots from the pre-baked image and is task-ready in < 10 s; it can run a streaming completion against the wired alias `gemma` (a request for a retired catalog alias is refused explicitly, never substituted); an unknown/ unavailable model is reported explicitly (never substituted silently); the VM cannot reach GCP; and the controller sees agent status **only** as C0 counters.

---

## 19. Appendix — frozen reference

### 19.1 Addresses, ports, env

| Item | Value | Source |
|------|-------|--------|
| Inference base URL (agents) | `http://10.10.0.13:8080` | D5 |
| Flash worker | `127.0.0.1:8081` | this doc §8.1 |
| Pro worker | `127.0.0.1:8082` | this doc §8.1 |
| Agent broker (per node) | `http://10.20.<n>.1:7071` | this doc §6 |
| Controller REST | `https://<controller-host>/v1` | controller-api §1.1 |
| Node guest bridges | `10.20.1.0/24`, `10.20.2.0/24`, `10.20.3.0/24` | D2 |
| Wired model aliases | `gemma` (AR-6); catalog distills remain baked, not wired | governance §7.1 |

### 19.2 `/etc/termini/agent.yaml` (canonical example)

```yaml
project_id: 01M4349RPSZFTB60AVJEG0ZZY9
vm_id: 01M4349RPXWDC52ZDAVMPCMJSH
node_id: 01M4349RPTT29RNTC6VBCXQ0RC
agent_image_version: agent-vm-2026.10.0+g1ab2c3d
inference:
  base_url: http://10.10.0.13:8080
  default_model: gemma
  allowed_models: [gemma]
  token_file: /etc/termini/inference.token
  stream: true
  connect_timeout_s: 2
  request_timeout_s: 120
broker:
  endpoint: http://10.20.1.1:7071
  pull_interval_s: 1
  max_concurrent_tasks: 2
tasks:
  max_concurrent: 2
  result_sink: /var/lib/termini/agent/results
logging:
  sink: /var/log/termini/agent.jsonl
  local_only: true
governance:
  telemetry_class: C0
  egress: deny
```

### 19.3 `/v1/models` response (canonical example)

```json
{
  "object": "list",
  "data": [
    { "id": "gemma", "object": "model", "created": 1791100000,
      "owned_by": "termini", "residency": "resident", "context_length": 16384 }
  ]
}
```
The listing contains **only wired aliases** (AR-6), so today it is exactly this one entry. The
catalog distills are baked in the image but are not served, so they do not appear here.
