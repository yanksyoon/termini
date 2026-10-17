# Termini — Node Bootstrap & Cluster-Join Contract (W1)

- Owner: `infra` (kanban task `t_134e2f24`)
- Status: **FROZEN** for the 5-day build.
- Date: 2026-10-04
- Audience: controller, agents, frontend, governance, verifier workstreams; anyone provisioning Nodes A/B/C.
- Related: `docs/contracts/infra-decisions.md` (this workstream — decisions D1–D8), `docs/contracts/controller-api.md`, `docs/contracts/node-telemetry.md` (controller), `docs/contracts/agent-runtime.md` (agents), `docs/contracts/governance.md` (governance), `docs/architecture.md` (architect).
- Requirements source: `~/termini/project.md`.

**Scope of this document.** It specifies the end-to-end contract by which a **customer-owned node** (A, B or C) goes from a pre-baked image to a fully registered, cluster-joined, telemetry-reporting member of the Termini fleet: image inputs, the registration handshake, the LXD cluster join sequence, telemetry prerequisites, SSH key injection, failure/retry behaviour, and how to verify it works. Addresses, ports, storage, and the control topology are fixed by `infra-decisions.md` and are **not** restated as choices here.

**Reconciliation note.** Endpoint paths, field names, and error codes below are the **node-side canonical view** and were proposed to match the controller workstream. The controller workstream (`controller-api.md`, `node-telemetry.md`) owns the full controller API surface; where the two ever disagree, `docs/architecture.md` is the tie-breaker and both files must be updated to match it. No field in this document is left as TBD.

**Implementation status (2026-10-04).** The `termini-bootstrap` daemon, the
node-side redactor, and every node-local artifact specified in this document
(the `termini-bootstrap` binary/unit, the LXD join automation, the telemetry
spool, `termini status`) are a **specification, not shipped code**: this repo
contains no daemon source, systemd unit, or node redactor. For the demo the node
side is a simulator (`chaos/node_sim.py`), and content-based (C2) governance is
enforced **controller-side** — a non-conforming telemetry batch is rejected whole
with `422 GOVERNANCE_REJECT`, with no on-node "drop the field before the socket
opens" step. §12 below describes the target acceptance state and is not yet
runnable in this build.

---

## 1. Actors and topology

| Component | Runs where | Responsibility |
|-----------|------------|----------------|
| `termini-bootstrap` daemon | Every node (systemd, pre-installed) | The **only** node component that talks to the controller. Reads config, gathers hardware facts, registers, joins LXD, pushes telemetry, executes controller commands. |
| GCP Controller | `10.10.0.10` | Validates enrollment, assigns roles, issues node client certs + LXD join tokens, ingests telemetry, drives remediation, brokers terminal. |
| LXD cluster `termini` | Nodes A/B/C | Hosts agent VMs; seed = Node A. |
| Node C inference service | Node C host, `:8080` | `llama.cpp` OpenAI-compatible endpoint (host-exposed, per D5). |

All node↔controller traffic is **outbound** from the node over mTLS (D6). The node needs no inbound exposure to operate.

The bootstrap daemon runs as `termini-bootstrap.service` under a dedicated unprivileged user `termini`, with tightly scoped sudoers rules (it must call `lxd init`, manage `nftables`, and read `nvidia-smi`). It never runs arbitrary controller-supplied shell; controller commands are a fixed, typed verb set.

---

## 2. Bootstrap state machine

The daemon publishes its state locally (`termini status`) and in telemetry.

```
                 image boot
                     │
                     ▼
              ┌─────────────┐  no config / no token
              │  ACTIVATING │──────────────────────────► stays ACTIVATING (waits)
              └─────────────┘
                     │ config present + clock OK
                     ▼
              ┌─────────────┐  token rejected (terminal)   ┌────────────────────┐
              │  REGISTERING│─────────────────────────────►│ REGISTRATION_FAILED│
              └─────────────┘                              └────────────────────┘
                     │ 2xx + client cert
                     ▼
              ┌─────────────┐  join fails after retries
              │   JOINING   │──────────────────┐
              └─────────────┘                  ▼
                     │ join ok            ┌──────────┐
                     ▼                    │ DEGRADED │◄── cluster/GPU/disk problems
              ┌─────────────┐  <──────────└──────────┘   (still telemetrying)
              │   ONLINE    │
              └─────────────┘
```

- **ONLINE** = registered, in the cluster (or intentionally standalone by controller instruction), telemetry flowing, host healthy.
- **DEGRADED** = reachable and reporting, but one or more capabilities are unavailable (e.g. not joined, GPU down, disk degraded). **A degraded node must still report telemetry** — this is what lets the AI operator remediate it.
- **REGISTRATION_FAILED** = a terminal enrollment problem requiring operator action — a bad/consumed/expired token (401/403), a malformed registration (400/422), an unsupported schema (426), or a controller that is unreachable after the bounded boot retries (row 1). It is **not** retried indefinitely (see §9). The daemon parks in this state (no auto-retry) and records `failure_kind` in `state.json`; `systemd` never sees `READY`.
- **LXD_API_UNAVAILABLE** = the machine is enrolled in principle but **cannot be remediated**: its own LXD REST API does not answer on the node's own `host:port` and could not be brought up (`failure_kind = lxd_api_unreachable`, §9 row 14). Distinct from `REGISTRATION_FAILED` on purpose — the credential is fine, the hypervisor is not, so an operator looks at LXD. The daemon parks here (no auto-retry, no `READY`) and the enrollment token is **not** spent: no registration request is made.

---

## 3. Image inputs

### 3.1 Host image ("Termini Node OS")

Built by CI (Packer or `virt-customize`), published as a GCP machine image and a `qcow2`, tagged `<YYYY.MM.DD>-<gitsha>` (see D3). Contains, **already installed**:

- Ubuntu Server 24.04 LTS minimal; `systemd`; `chrony`; `nftables`; `openssh-server`.
- **LXD snap**, channel `5.21/stable`, revision pinned; `lxd` service **not** initialised (no cluster yet).
- **`termini-bootstrap`** binary + unit, enabled; a marker file `/var/lib/termini/.unconfigured` present so the daemon waits for config.
- **Preloaded workload images** in the local LXD image store, imported from on-disk tarballs (no registry pull at boot):
  - `agent-vm/<version>` — the agentic OS guest.
  - `inference/<version>` — `llama.cpp` runtime (present for completeness; not launched in the demo per D5).
- Node C variant only: the NVIDIA driver stack (`nvidia-driver-*`) + `nvidia-smi`, installed and loaded at build time.

No `apt`/network fetch happens at boot.

### 3.2 Per-node inputs (cloud-init `user-data` / instance metadata)

The controller renders a small `user-data` (or GCP instance metadata keys) per node. **These are the only inputs boot needs.**

| Field | Required | Type | Meaning |
|-------|----------|------|---------|
| `termini.controller_url` | yes | string (https URL) | e.g. `https://controller.termini.example` on port 443. |
| `termini.enrollment_token` | yes | string (single-use, JWT) | Short-TTL (< 1 h), single-use, optionally bound to an expected machine ID/role. |
| `termini.ca_pubkey` | yes | string (Ed25519 public key) | Controller's SSH CA public key (D4); installed as `/etc/ssh/termini_ca.pub` and wired as the `TrustedUserCAKeys` trust anchor so the node accepts controller-signed short-lived user certs. |
| `termini.node_name` | yes | string | Cluster member name, e.g. `node-a`. |
| `termini.role_hint` | no | enum `auto\|compute\|inference` | Advisory only; the controller assigns the authoritative role. Default `auto`. |
| `termini.underlay_ip` | yes | string (CIDR) | Static underlay address, e.g. `10.10.0.11/24`. |
| `termini.underlay_gw` | yes | string | Default gateway. |
| `termini.bootstrap_seed_ip` | conditional | string | Underlay IP of the LXD seed (`10.10.0.11`). Required for joiners (B/C); optional on the seed. |
| `termini.breakglass_pubkey` | no | string (SSH pubkey) | One-time operator recovery key, **removed after successful registration** (see §7). |
| `termini.telemetry_interval_ms` | no | int | Initial cadence; overridden by the registration response. Default 15000; demo 1000. |

The enrollment token may alternatively be delivered as a file `/var/lib/termini/enrollment.token`. Cloud-init writes config to `/etc/termini/config.yaml` (schema in §13).

### 3.3 Example `user-data`

```yaml
#cloud-config
write_files:
  - path: /etc/termini/config.yaml
    permissions: '0600'
    owner: root:root
    content: |
      controller_url: https://controller.termini.example
      node_name: node-b
      role_hint: auto
      underlay_ip: 10.10.0.12/24
      underlay_gw: 10.10.0.1
      bootstrap_seed_ip: 10.10.0.11
      telemetry_interval_ms: 1000
  - path: /var/lib/termini/enrollment.token
    permissions: '0600'
    owner: root:root
    content: |
      eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.PLACEHOLDER.SIGNATURE
  - path: /etc/ssh/termini_ca.pub
    permissions: '0644'
    owner: root:root
    content: |
      ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI... termini-ssh-ca
runcmd:
  - [ systemctl, enable, --now, termini-bootstrap.service ]
```

The `ca_pubkey` also configures `sshd`:

```
# /etc/ssh/sshd_config.d/termini.conf
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
TrustedUserCAKeys /etc/ssh/termini_ca.pub
```

---

## 4. Node identity

- **Stable identity** = `machine_id` = `sha256(dmi.product_uuid + ":" + /etc/machine-id)`. This is the dedup/upsert key on the controller.
- **Node-local keypair**: at first boot the daemon generates an **Ed25519** keypair at `/var/lib/termini/identity/node.key` (`0600`, root:termini). The **private key never leaves the node**.
- **Registration** carries a **CSR** (`node.key`), not the key. The controller returns a **signed client certificate** (`/var/lib/termini/identity/node.crt`) used for the mTLS control channel (D6).
- **Host SSH public keys** are reported at registration for pinning (§7).
- Identity survives reboot. `termini reset` deletes `/var/lib/termini/identity/` and forces re-enrollment (§10).

---

## 5. Registration handshake

### 5.1 Sequence

```
Node bootstrap                                          Controller
     │                                                       │
     │ 0. clock synced (chrony), config loaded              │
     │                                                       │
     │ 1. POST /v1/nodes/register   (Bearer enrollment_token, TLS, plain HTTP/443) 
     │    body: NodeRegistration (machine facts + CSR)  ───►│ validate token (single-use, TTL,
     │                                                       │ bound machine/role), upsert by machine_id
     │                                                       │ assign role, mint client cert, mint
     │                                                       │ LXD join token if joiner
     │ 2. 201 NodeRegistrationResponse                      │
     │    ◄── node_id, role, cluster plan, client cert,     │
     │        telemetry cfg, controller_time                │
     │                                                       │
     │ 3. persist identity; run LXD seed/join (§6)          │
     │                                                       │
     │ 4. OPEN wss://controller/node/v1/{node_id}/channel (mTLS)│
     │    ◄──► heartbeat + telemetry + command/response      │
```

Step 1 uses the enrollment token because the node has no cert yet. TLS certificate validation on first contact uses a **pinned controller CA** shipped in the host image (not system trust), so an on-prem node does not depend on public CA reachability for enrollment. All later traffic is mTLS with the issued node cert.

### 5.2 Request — `POST /v1/nodes/register`

`Authorization: Bearer <enrollment_token>`, `Content-Type: application/json`.

```json
{
  "schema_version": "1.0",
  "machine": {
    "machine_id": "9f2c1e...e7",
    "dmi_product_uuid": "4c4c4544-...",
    "hostname": "node-b",
    "image_version": "2026.10.01-abc1234",
    "os": { "name": "ubuntu", "version": "24.04.1" },
    "lxd": { "version": "5.21.3", "snap_revision": "31234", "endpoint": "10.10.0.12:8443" },
    "cpu": { "model": "Intel Xeon ...", "sockets": 1, "cores": 8, "threads": 16 },
    "memory": { "total_bytes": 68719476736 },
    "disks": [
      { "device": "/dev/sda", "type": "ssd", "size_bytes": 214748364800, "health": "ok" }
    ],
    "nics": [
      { "name": "ens4", "addresses": ["10.10.0.12/24"], "role": "underlay" }
    ],
    "gpus": [
      { "vendor": "nvidia", "model": "Tesla T4", "vram_bytes": 17179869184,
        "pci_id": "0000:00:04.0", "driver": "535.183", "health": "ok" }
    ],
    "underlay_ip": "10.10.0.12",
    "role_hint": "auto"
  },
  "node_pubkey_csr_pem": "-----BEGIN CERTIFICATE REQUEST-----\n...\n-----END CERTIFICATE REQUEST-----\n",
  "host_ssh_keys": [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI... root@node-b"
  ],
  "agent": { "version": "0.1.0", "boot_id": "8f0e..." }
}
```

`machine.gpus` is `[]` on compute nodes and on Node C when the driver/GPU is unavailable (the node still registers; see §9).

`machine.lxd.endpoint` is the node's **own LXD REST address** (`host:port`, or a
full `scheme://host:port`). The controller needs it to place agent VMs on the
right node and to execute an operator action — a single controller-wide URL cannot
name one node among a fleet (`infra-decisions.md` D2). **A node advertises it only
after proving something answers on it** (`GET /1.0` over HTTPS on its own primary
IPv4), and `POST /v1/nodes/register` **fails closed** when that listener cannot be
brought up: the daemon re-asserts `core.https_address` through its own unix socket,
waits a bounded window, and if the API is still dead it never sends the request —
it parks in `LXD_API_UNAVAILABLE` (§2, §9 row 14) and does not spend the enrollment
token. It is still *optional* on the wire (an older node that omits it registers and
the controller falls back to its global `TERMINI_LXD_BASE_URL`), but a node that
reports one always reports a live one. A node that comes up later, or whose address
changes, re-reports it on the heartbeat (§8), so the controller's per-node endpoint
never has to fall back for a node that has one.

`machine.disks[].size_bytes` is the **measured** size of the device. Report the
real value, or OMIT the field when it cannot be measured: the controller then
records that node's disk as *unknown* (`disk_known = false`) rather than as zero
disk. A node build previously sent a hardcoded `size_bytes: 1`, which reads as
0 GiB, so the per-node disk ceiling was silently never enforced for any real node.

**Amendment (demo provenance — companion to `controller-api.md` §5.2 step 5).**
`POST /v1/nodes/register` also accepts an **optional top-level boolean
`synthetic`** (alias `registration_source: "seeder"`). Real hardware never sends
it. The demo seeder (`console/seed_demo.py`, systemd unit `termini-seeder`) sets
`"synthetic": true` on every registration it makes, because it registers through
this same endpoint with the same single-use bootstrap tokens a node uses — so
without the marker a fabricated row is indistinguishable from real hardware, and
`auto` target-node resolution, having only reported capacity to go on, picked the
seeder's inflated facts (64 GiB) over the real node's real ones (1-8 GiB) and
allocated agents onto a node that does not exist. The controller stores the flag
(`nodes.synthetic`, migration `0011`), never places an allocation on a fabricated
row while any real node is registered, and surfaces it in the node view
(`controller-api.md` §4.2). An absent, wrong-typed, or unrecognised value reads as
*real* — the safe direction.

---

### 5.3 Response — `201 Created` `NodeRegistrationResponse`

Canonical response (agreed with `controller-api.md` §4.1; the controller adds the
`project_id` / `node_token` / `config` fields the node needs for REST-fallback auth
and project scoping):

```json
{
  "schema_version": "1.0",
  "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
  "project_id": "01M4349RPSZFTB60AVJEG0ZZY9",
  "role": "compute",
  "node_token": "opaque-node-secret",
  "identity": {
    "client_cert_pem": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n",
    "ca_chain_pem": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n"
  },
  "cluster": {
    "name": "termini",
    "role": "joiner",
    "member_name": "node-b",
    "server_address": "10.10.0.11:8443",
    "join_token": "eyJzZX...ZSI6...joiner-token"
  },
  "telemetry": { "interval_ms": 1000, "channel": "wss://controller.termini.example/node/v1/01M4349RPTT29RNTC6VBCXQ0RC/channel" },
  "config": { "heartbeat_interval_s": 5, "telemetry_interval_ms": 1000, "telemetry_batch_max": 200, "command_channel": "websocket" },
  "controller_time": "2026-10-04T07:59:00Z",
  "registration_ttl_s": 3600
}
```

- Seed node response: `cluster.role = "seed"`, `join_token = null`.
- `role = "inference"` only for Node C.
- `node_token` authenticates the REST fallback endpoints (`controller-api.md` §4.1); `identity` drives the mTLS control channel (D6).
- `registration_ttl_s` bounds how long the issued client cert is valid before the daemon must re-issue over the authenticated channel.

### 5.4 Errors

| Status | `code` | Meaning | Node behaviour |
|--------|--------|---------|----------------|
| 400 | `INVALID_ARGUMENT` | Schema/parse failure | Log, backoff-retry up to 3, then `REGISTRATION_FAILED`. |
| 401 | `UNAUTHENTICATED` | Missing/malformed/expired/consumed token | Terminal → `REGISTRATION_FAILED` (no blind retry; §9). |
| 403 | `PERMISSION_DENIED` | Token bound to a different machine/role | Terminal → `REGISTRATION_FAILED`. |
| 409 | `CONFLICT` | `machine_id` exists | If body cert is valid → treat as reconnect; else server returns existing `node_id` and the node performs §10 reconnect. |
| 426 | `UNSUPPORTED_VERSION` | `schema_version` unsupported | Terminal; alert operator (image/controller skew). |
| 429 | `RATE_LIMITED` | Too many attempts | Honour `Retry-After`; backoff. |
| 5xx | `INTERNAL` | Controller error | Backoff-retry indefinitely (bounded by max backoff 60 s). |

Error codes are the canonical UPPER_SNAKE set from `controller-api.md` §3. All error
bodies use the controller error envelope: `{ "error": { "code", "message", "details", "request_id" } }`.

---

## 6. LXD cluster join sequence

The controller drives the cluster through this contract; the daemon executes and reports. Cluster config is identical across members except member-specific values.

### 6.1 Seed (Node A, first boot)

Response `cluster.role = "seed"`. Daemon writes and applies:

```yaml
# /var/lib/termini/lxd-preseed-seed.yaml
config:
  core.https_address: 10.10.0.11:8443
cluster:
  server_name: node-a
  enabled: true
networks:
  - name: lxdbr0
    type: bridge
    config:
      ipv4.address: 10.20.1.1/24
      ipv4.nat: "true"
      ipv6.address: none
      bridge.mtu: "1460"
storage_pools:
  - name: local
    driver: zfs
    config:
      source: /var/snap/lxd/common/lxd/disks/local.img   # demo: loop-backed; on-prem: /dev/disk/by-id/...
      size: 60GiB
profiles:
  - name: termini-base
    description: Termini base profile
    config:
      limits.cpu: "8"
      limits.memory: 16GiB
    devices:
      root:
        path: /
        pool: local
        type: disk
      eth0:
        name: eth0
        network: lxdbr0
        type: nic
```

Apply: `lxd init --preseed < /var/lib/termini/lxd-preseed-seed.yaml`.
Then mark the seed ONLINE and obtain join tokens for members:

```
POST /1.0/cluster/members   {"server_name":"node-b","password":"<generated>"} / or `lxc cluster add node-b`
```

The controller mints a token per joiner and returns it in that joiner's registration response (`cluster.join_token`).

### 6.2 Joiner (Nodes B, C)

Response `cluster.role = "joiner"`. Daemon writes and applies:

```yaml
# /var/lib/termini/lxd-preseed-join.yaml
cluster:
  server_name: node-b
  enabled: true
  server_address: 10.10.0.11:8443
  cluster_token: eyJzZXJ2ZXJfbmFtZSI6...joiner-token
```

Apply: `lxd init --preseed < /var/lib/termini/lxd-preseed-join.yaml`.

**Preconditions the daemon enforces before joining** (mismatch here is the #1 cause of failed joins):
1. The node's LXD snap revision **equals** the seed's (`/1.0` `environment.server_version` / `snap_revision`). If not, the daemon **does not attempt the join** and goes DEGRADED with `cluster_reason: lxd_version_mismatch`.
2. `core.https_address` set to the node's underlay IP:8443 and reachable from the seed.
3. Storage pool `local` configured with the same driver (`zfs`) — per-member local pools are allowed; pool *name and driver* must match.
4. Network `lxdbr0` uses this member's guest subnet (10.20.2.0/24 / 10.20.3.0/24) with the same `networks` entry shape.
5. Time synced (§8).

After `lxd init --preseed` succeeds, the daemon verifies membership via `GET /1.0/cluster/members` (or `lxc cluster list`) and reports ONLINE with its member name.

### 6.3 Controller's cluster view

The controller mints cluster join tokens at registration (seed API
`POST /1.0/cluster/members` on `https://10.10.0.11:8443` with an LXD trust client
cert) — cluster administration only. **Agent-VM placement is separate:** apply-plan
materialisation (`controller-api.md` §4.2) drives each allocation's resolved node's
LXD REST API (`POST /1.0/instances`) over that node's **own registered endpoint**
(`machine.lxd.endpoint`, §5.2); the controller uses the global
`TERMINI_LXD_BASE_URL` only for a node that reported no endpoint. This is a
deliberate divergence from AR-2 (`agent-runtime.md` §4.1, a node-daemon-mediated
`vm.launch`): the demo places work straight from the controller, so a node must
advertise the address placement should use. The seed's `8443` cluster-admin trust
cert is distinct from this per-node placement path. Member health is folded into
node telemetry.

---

## 7. SSH key injection and host-key pinning

Injected at provisioning (cloud-init):

1. **Controller SSH CA public key** → installed as `/etc/ssh/termini_ca.pub` and wired
   as `TrustedUserCAKeys`, so the node accepts the controller-signed short-lived user
   certificates (D4). This is the shipped trust anchor, not a placeholder.
2. **Break-glass operator key** (optional) → `/root/.ssh/authorized_keys`, tagged with a marker comment. The daemon **removes that line after successful registration**; it is a one-time recovery path, not standing access.
3. `PasswordAuthentication no`, `PermitRootLogin prohibit-password`, SSH bound to the underlay interface.

At registration (step 1 above) the daemon reports `host_ssh_keys`. The controller records them and thereafter verifies host identity on every SSH the platform makes (no blind trust). A host-key change after registration raises a controller alert.

Interactive user access is **not** distributed as a private key. The operator (or
dashboard) generates a keypair **locally in the browser**, uploads only the public half,
and the controller signs a **short-lived (≤8 h), principal-bound certificate**
(`POST /v1/projects/{project_id}/ssh-key`, `controller-api.md` §4.3). The dashboard shows
a one-click web terminal plus an `ssh` string (`dashboard.md` §3.2), and the xterm.js
terminal is **controller-brokered** over the node's outbound channel (D6), so no inbound
SSH is required.

---

## 8. Telemetry prerequisites

The node must satisfy these **before** it counts as ONLINE:

- **Time**: `chrony` synced; daemon waits (bounded, §9) for `|skew| ≤ 5 s` because enrollment tokens and certs are time-bound. Reports its measured skew in the first heartbeat.
- **Egress**: outbound TCP/443 to `termini.controller_url` allowed by the node's `nftables` default-deny policy. No inbound rule is required.
- **Spool**: `/var/lib/termini/telemetry/` exists, writable by `termini`, bounded (default 5000 samples / ~5 MiB ring).
- **Metrics sources readable by the daemon**: `/proc`, `/sys`, the LXD unix socket (`lxd` group membership), and — on Node C — `nvidia-smi`.
- **Cadence**: initial value from config; authoritative value from the registration response (`telemetry.interval_ms`; demo 1000, default 15000).
- **Channel**: the daemon opens `wss://.../channel` with its client cert and streams heartbeats + telemetry; see `docs/contracts/node-telemetry.md` for the payload schema (owned by the controller workstream).
- **Governance**: telemetry contains **hardware/OS facts only**. Application logs, agent payloads, file contents, and user prompts are never emitted by the daemon (see `docs/contracts/governance.md`).

---

## 9. Failure and retry semantics

Guiding rules: **registration and join failures must never leave a node unreachable or unbootable**, and **a degraded node keeps reporting** so the AI operator can act. `systemd` runs the unit with `Restart=always`, `RestartSec` backoff, and a start-rate limit that surfaces a crash loop as `failed`.

| # | Failure | Detection | Node behaviour | Resulting state |
|---|---------|-----------|----------------|-----------------|
| 1 | Controller unreachable | connect timeout / DNS | **Boot — fail fast:** retry ≤ 3 (backoff 1 s → 4 s) then **stop auto-retry** and park terminal; the state is `DEGRADED` while backing off (never a silent `REGISTERING` hang) and `failure_kind` records `unreachable_dns` / `unreachable_refused` / `unreachable_timeout`. **Steady, after enrollment — retry forever** at 1 s → 60 s cap; local services (LXD) still start; **no data egress**. | boot: `REGISTRATION_FAILED`; steady: `DEGRADED` |
| 2 | Enrollment token invalid/expired/consumed | HTTP 401/403 | Retry at most 3 times with backoff, then stop auto-retry; emit local alert + telemetry if any channel exists. Requires operator re-provision with a fresh token. | `REGISTRATION_FAILED` |
| 3 | Controller 5xx during registration | HTTP 5xx | Backoff-retry indefinitely. | `REGISTERING` |
| 4 | `machine_id` already registered (409) | HTTP 409 | If a valid local cert exists → reconnect (§10); else consume server-provided `node_id` and reconnect. | `ONLINE` |
| 5 | LXD seed unreachable during join | TCP/8443 timeout | Retry join ≤ 5 with backoff (5 s → 60 s). | `JOINING` → `DEGRADED` |
| 6 | LXD join rejected (version/pool/network mismatch) | LXD error | Do not loop. Mark node standalone-capable, report `cluster_reason`, **keep telemetry flowing**. | `DEGRADED` |
| 7 | Cluster joined but LXD API errors | LXD API probe fails | Report `lxd_unhealthy`; controller raises an incident. | `ONLINE` (unhealthy) |
| 8 | Telemetry channel drops | WS close | Reconnect with backoff; spool metrics locally; flush on reconnect; drop oldest when ring is full. | `ONLINE` (spooling) |
| 9 | Clock skew too large | `|skew| > 5 s` after 120 s wait | Proceed with a `clock_skew_warn` flag; token validation may fail and is handled as #2. | `ONLINE` (warn) |
| 10 | GPU/driver unavailable (Node C) | `nvidia-smi` fails / `gpus=[]` | **Registration still succeeds**; report `health: gpu_unavailable`; controller re-routes inference / alerts. | `DEGRADED` |
| 11 | Disk degraded / read-only | SMART + mount probe | Report disk health; controller decides evacuation (Scenario B). | `ONLINE`/`DEGRADED` |
| 12 | Daemon crash loop | systemd start-rate limit | Unit enters `failed`; last state persists in `/var/lib/termini/state.json` for the operator. | `failed` (systemd) |
| 13 | Reboot after successful enrollment | boot | Idempotent reconnect using persisted identity (§10). | `ONLINE` |
| 14 | **Own LXD API not listening** (`core.https_address` empty, daemon crashed, listener drifted away) | `GET /1.0` on the node's own `host:port` fails | Re-assert `core.https_address` over the daemon's unix socket (start the unit when the socket is dead) and re-probe for a bounded window. If it still does not answer: **do not register** (the enrollment token is not spent), do not report `READY`, park terminal with `failure_kind = lxd_api_unreachable`. On a rebooted node the same check gates `READY` before reconnect. | `LXD_API_UNAVAILABLE` |

**Bounded retries, and what "terminal" means (implemented in
`infra/bootstrap/node/termini-bootstrap.py`).** Rows 1 (boot), 2 and 5.4's
`400 INVALID_ARGUMENT` / `426 UNSUPPORTED_VERSION` are **bounded**: after at most
`registration_max_retries` (default 3) backoff retries the daemon stops
auto-retrying and parks in `REGISTRATION_FAILED`. Because the unit is
`Restart=always`, exiting would let systemd respawn the daemon straight back into
the retry loop; a genuinely terminal failure therefore **holds the process** in
the terminal state (READY is never sent, `state.json` stays observable, and a
`systemctl restart` / `termini reset` is required to retry). Transient failures
(5xx, 429, and a steady-state unreachable controller after enrollment) keep
backing off 1 s → 60 s indefinitely. `failure_kind` in `state.json` names the
class (`auth`, `schema`, `unsupported`, `conflict`, `malformed`, `unreachable_*`).

**Amendment (2026-10-04, task `t_ca7fc132`).** Row 1 previously specified that a
*boot-time* unreachable controller retried forever in `ACTIVATING`/`DEGRADED`,
which left a mis-provisioned node (bad DNS, dead controller) silently stuck and
invisible to the demo operator. The boot path now fails fast to
`REGISTRATION_FAILED` as above; the steady-state path is unchanged. See
`w7/redteam/cloud-init-boot-latency.md` §Resolution for the measured evidence.

**Never** does a bootstrap failure cause the daemon to wipe LXD or reformat, and **never** does it silently stop reporting while the machine is alive.

---

## 10. Idempotency and re-enrollment

- **Key**: `machine_id`. The controller **upserts**; re-registering the same machine never creates a duplicate node.
- **Reboot**: if `/var/lib/termini/identity/node.crt` is present and valid, the daemon performs `POST /v1/nodes/{node_id}/reconnect` (mTLS) instead of `/register`. It re-joins LXD only if membership is missing.
- **Cert expired**: re-issue over the authenticated channel (or, if the channel is unavailable, fall back to `/register` with a fresh token).
- **Manual reset** (`termini reset`): deletes `/var/lib/termini/identity/`, stops the daemon, reverts LXD to unconfigured *only if the node is not a cluster member* (a member must be removed via the seed first). Next boot re-enrolls and needs a fresh token.
- **Reimage**: if the DMI UUID is unchanged, identity is preserved; a changed machine ID is treated as a new node (documented so operators don't churn node records).
- **Re-registration after a lost spool** is safe: telemetry is idempotent by `(node_id, timestamp)`.

---

## 11. Security and governance constraints (node side)

- The bootstrap daemon is the **sole egress path** for platform data; it emits **hardware/OS telemetry only**.
- Controller commands are a **fixed typed verb set** — the `commandAllowlist()` from `controller-api.md` §6. The verbs actually wired in the demo are the human node actions `node.reboot`/`node.drain` plus the AI-operator's LXD REST actions (`instance.restart`, `instance.stop`, `instance.delete`, `image.prune`); the broader `vm.resize`/`vm.migrate`/`vm.stop`/`vm.start`/`vm.destroy`/`agent.dispatch_task`/`agent.cancel_task` set is declared in `controller/app/commands.py` but not wired (future/day-2), and `vm.launch` has no launch path — apply-plan materialisation instead drives the resolved node's LXD REST API directly from the controller (`_apply_plan` creates the instances and returns their ids; `controller-api.md` §4.2). The daemon validates and executes known verbs only; it never `eval`s controller strings. Telemetry cadence is set via the registration response (not a command); health probes are folded into telemetry.
- Enrollment tokens are single-use and short-TTL; the enrollment endpoint is the only unauthenticated (token-auth) controller call a node makes.
- No private keys transit the controller (user keys stay in the browser; node keys stay on the node — only CSR/public keys move).
- The break-glass SSH key is removed after registration (§7); standing SSH is the controller-held CA signing short-lived (≤8 h) principal-bound user certs, surfaced as a web terminal + `ssh` string (D4).
- Firewall: default-deny inbound; allow `22` from the admin CIDR, `8443` from cluster peers + controller, `8080` from the underlay on Node C only (D2).

---

## 12. Verification (acceptance test)

A fresh node is considered correctly bootstrapped when **all** of the following hold. Run from the sources noted.

1. **Node-local (on the node):**
   ```bash
   systemctl is-active termini-bootstrap          # active
   termini status                                  # state: ONLINE, node_id, role, cluster member name
   lxc cluster list                                # shows node-a, node-b, node-c all ONLINE
   ```
2. **From the controller:**
   ```bash
   curl -sS -H "Authorization: Bearer ***" \
     https://controller.termini.example/v1/projects/{project_id}/nodes | jq '.nodes[] | {node_id,role,status,last_seen_at}'
   curl -sS -H "Authorization: Bearer ***" \
     https://controller.termini.example/v1/projects/{project_id}/nodes/01M4349RPTT29RNTC6VBCXQ0RC | jq .metrics_latest
   ```
3. **Cross-node networking (from an agent VM on Node A) — the D2 acceptance:**
   ```bash
   curl -sS http://10.10.0.13:8080/v1/models      # Node C llama.cpp reachable from Node A's guest
   curl -sS -o /dev/null -w '%{http_code}\n' http://10.10.0.13:8080/health
   ```
4. **SSH custody (D4):**
   ```bash
   ssh-keygen -s ca -I demo -n root -V +8h ./demo.pub   # from the controller secret store only
   ssh -i <(cat demo demo.pub) root@10.10.0.11 'hostname'   # cert accepted; password auth refused
   ssh -o PreferredAuthentications=password root@10.10.0.11  # must fail
   ```
5. **Timing:** from `systemctl start termini-bootstrap` on a pre-baked node to `ONLINE` in the controller is **target < 5 s** (no package installs, no downloads).

The verifier workstream (`docs/contracts/w1-review.md`) may contrast these steps against the acceptance criteria for W1.

---

## 13. Reference: `/etc/termini/config.yaml`

```yaml
controller_url: https://controller.termini.example   # required
node_name: node-b                                     # required
underlay_ip: 10.10.0.12/24                            # required
underlay_gw: 10.10.0.1                                # required
bootstrap_seed_ip: 10.10.0.11                         # required for joiners
role_hint: auto                                       # auto|compute|inference
lxd_port: 8443                                        # LXD REST port reported as machine.lxd.endpoint
telemetry_interval_ms: 1000                           # overridden by registration response
enrollment_token_file: /var/lib/termini/enrollment.token
identity_dir: /var/lib/termini/identity
spool_dir: /var/lib/termini/telemetry
# Registration retry policy (§9). Optional; defaults shown.
register_timeout_s: 5                                 # per-attempt HTTP timeout (fail fast)
registration_max_retries: 3                           # bounded retries before terminal REGISTRATION_FAILED
registration_backoff_base_s: 1                        # first backoff
registration_backoff_cap_s: 60                         # backoff ceiling (transient failures)
heartbeat_interval_s: 15                               # REST heartbeat cadence; each beat re-reports
                                                       # machine.lxd.endpoint when the listener answers
```

Runtime state is kept at `/var/lib/termini/state.json` (state, node_id, last error, cluster reason, last controller contact).

---

## 14. Sequence diagram (boot → online)

```
 operator            node (image boot)              controller (GCP VM)
    │                      │                             │
    │ provision A/B/C      │                             │
    ├─────────────────────►│ cloud-init writes           │
    │  (image + user-data) │ /etc/termini/config.yaml    │
    │                      │ + CA pubkey + token         │
    │                      │                             │
    │                      │ systemd starts              │
    │                      │ termini-bootstrap           │
    │                      │  - chrony sync              │
    │                      │  - gather hw facts + CSR    │
    │                      │                             │
    │                      │ 1. POST /v1/nodes/register ─►│ validate token; assign role;
    │                      │                             │ mint client cert + join token
    │                      │ 2. ◄── 201 response ────────│
    │                      │ 3. persist identity         │
    │                      │ 4. lxd init --preseed       │
    │                      │    (seed then joiners)      │
    │                      │ 5. OPEN mTLS channel ──────►│
    │                      │ 6. ◄─► telemetry @1s ───────│
    │ dashboard: nodes     │                             │
    │ ◄────────────────────┴── ONLINE, cluster healthy ──┤
    │  one-click terminal / cert                           │
    │  inference base URL http://10.10.0.13:8080          │
```
