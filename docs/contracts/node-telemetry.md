# Node Telemetry Contract

Version 1.0 — frozen. Owner: controller team (consumer: AI operator loop + charts).

This document fixes the exact schema the node bootstrap daemon sends to
`POST /v1/nodes/{node_id}/telemetry` (transport, auth, and error handling in
`controller-api.md`). The invariant that governs every field:

**Only hardware and OS telemetry may leave the customer premises.** Application
logs, customer payloads, process argument vectors, file contents, environment
variables, and user prompts are NEVER collected and are rejected at ingest. The
redaction pipeline, C0/C1/C2 classification taxonomy, and egress allowlist are
normative in `governance.md`; this document uses those exact canonical field
names and marks every field with its C-class.

Field names are canonical (governance.md §4): implementations MUST use these
exact keys. Units are bytes (never GiB) and percent, per governance.md.

**This document is the single source of truth for the telemetry contract.** The
controller's enforced schema (`controller/app/fields.py`) mirrors it, and the
acceptance check (`controller/scripts/acceptance.py`) verifies the two agree on
every field name. There is no second telemetry schema document.

---

## 1. Transport and cadence

- Endpoint: `POST /v1/nodes/{node_id}/telemetry`, auth `node_token`.
- Transport: primary = the mTLS control channel `/node/v1/{node_id}/channel`
  (D6); this REST endpoint is the HTTP fallback when the WebSocket upgrade is
  blocked (`controller-api.md` §4.1).
- Cadence: one batch every `telemetry_interval_ms` (default **15,000 ms**; **1,000 ms**
  during the demo per `project.md` advisory #4 and D8).
- One batch may carry up to `telemetry_batch_max` (200) samples.
- The daemon sends a batch whenever (a) the interval elapses or (b) a
  metric-threshold trigger fires early (see § 5 Trigger).
- Response: `202 Accepted` with `{accepted, rejected, backpressure}`.
- Every egress payload passes the on-node redactor first and carries the
  `_redaction` metadata block (governance.md §5 step 7).

### 1.1 Backpressure

| Value         | Node action |
|---------------|-------------|
| `none`        | Continue normal cadence |
| `drop_metrics`| Skip non-essential metrics (charts only) for 5 s; keep heartbeat |
| `pause_5s`    | Halt telemetry for 5 s, then resume; heartbeat continues |

`rejected` counts samples dropped as out-of-order/duplicate `seq` within an
otherwise-valid batch — not validation rejections. Validation is **fail-closed,
whole-batch**: a governance or schema violation in ANY sample rejects the entire
batch (see §7), so a rejected sample never egresses (fail-closed per governance.md
§6). A sample that fails the node-side redactor is dropped on the node and never
egresses.

---

## 2. Batch envelope

Every batch is a single JSON object:

```json
{
  "_redaction": { "version": "redactor-config v3", "config_hash": "3f2a…", "result": "ok" },
  "call_id": "01M4349RPY26W8Q453PZYK05CB",
  "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
  "schema_version": 1,
  "governance_chain_tip": "01M4349RPZ4DMD3MT18EBZRQB4",
  "samples": [ { "...": "one Sample object per § 3" } ]
}
```

| Field | C-class | Type | Required | Notes |
|-------|---------|------|----------|-------|
| `_redaction` | C0 | object | yes | `{version, config_hash, result}`; result is `"ok"` in any payload that was actually sent (a `blocked` payload never egresses). See governance.md §5/§8.1. |
| `call_id` | C0 | string (ULID) | yes | Unique per batch; matches the node's on-node audit record. |
| `node_id` | C0 | string (ULID) | yes | Must match path param or `403`. |
| `schema_version` | C0 | integer | yes | Always `1`. |
| `governance_chain_tip` | C0 | string | yes | Hash-chain tip; controller reconciles against its parallel chain (governance.md §8.3). |
| `samples` | — | array | yes | 1..200 `Sample` objects. |

---

## 3. Sample schema

One `Sample` is a timestamped flat object using governance.md's canonical C0/C1
field names. All fields are optional EXCEPT `ts`, `seq`, and at least one metric
field. Units are explicit and fixed.

```json
{
  "seq": 1000,
  "ts": "2026-10-04T07:49:00.000Z",
  "uptime_sec": 3600,
  "cpu_util_pct": 31.2,
  "cpu_core_count": 16,
  "ram_total_bytes": 34359738368,
  "ram_used_bytes": 19756849561,
  "swap_used_bytes": 0,
  "load_avg_1m": 1.42,
  "cpu_temp_celsius": 51.0,
  "os_name": "Ubuntu",
  "os_version": "24.04",
  "kernel_version": "6.8.0-142-generic",
  "lxd_cluster_role": "member",
  "hostname": "5f2a9c1e7b4d4a1f",
  "ip_local": "10.0.0.0/24",
  "mac_address": "9c1e7b4d4a1f5f2a",
  "project_id": "01M4349RPSZFTB60AVJEG0ZZY9",
  "disk": [
    { "path": "/dev/sda", "total_bytes": 274877906944, "free_bytes": 214748364800,
      "write_latency_ms": 3.0, "mount_ro": false }
  ],
  "net": [
    { "iface": "eth0", "rx_bytes": 123000, "tx_bytes": 11000, "latency_ms": 0.8 }
  ],
  "gpu": [
    { "model": "NVIDIA L4", "util_pct": 88.0, "vram_total_bytes": 25769803776,
      "vram_used_bytes": 12884901888, "temp_celsius": 62.0 }
  ],
  "lxd_instances": [
    { "vm_id": "01M4349RPXWDC52ZDAVMPCMJSH", "state": "running",
      "cpu_util_pct": 12.0, "ram_used_bytes": 3328599654,
      "ram_limit_bytes": 4294967296, "disk_used_bytes": 2362232012,
      "state_age_sec": 812, "restart_count": 1 }
  ],
  "inference": {
    "server_up": true,
    "model": "gemma",
    "active_requests": 3,
    "queue_depth": 0,
    "tokens_per_s": 48.0,
    "vram_used_bytes": 12884901888
  },
  "services": [
    { "name": "termini-bootstrap", "status": "running",
      "state_age_sec": 3610, "restart_count": 0, "last_exit_code": 0,
      "journal_errors": 0, "journal_warnings": 2 },
    { "name": "lxd", "status": "running",
      "state_age_sec": 3610, "restart_count": 0 },
    { "name": "sshd", "status": "running",
      "state_age_sec": 3610, "restart_count": 0, "last_exit_code": 0,
      "journal_errors": 0, "journal_warnings": 0 },
    { "name": "llama-cpp", "status": "running",
      "state_age_sec": 900, "restart_count": 2, "last_exit_code": 137,
      "journal_errors": 1, "journal_warnings": 4 }
  ],
  "processes": [
    { "pid": 4187, "name": "llama-server", "state": "running",
      "cpu_util_pct": 41.5, "ram_used_bytes": 3328599654,
      "threads": 9, "age_sec": 900 },
    { "pid": 921, "name": "systemd-journal", "state": "sleeping",
      "cpu_util_pct": 0.4, "ram_used_bytes": 41943040,
      "threads": 1, "age_sec": 3610 }
  ]
}
```

---

## 4. Field reference (allowlist)

Every field below is tagged with its C-class from governance.md §4. Numbers are
floats unless marked integer. A present field that violates its type/range/enum
causes the sample to be rejected.

### 4.1 Host metrics — C0 (governance.md §4.1, exact names)

| Field | Type | Unit | Range | Description |
|-------|------|------|-------|-------------|
| `ts` | string RFC3339 | — | — | Sample timestamp |
| `seq` | integer | — | ≥ 0, strictly increasing | Monotonic sample counter |
| `uptime_sec` | integer | s | ≥ 0 | Host uptime |
| `cpu_util_pct` | number | percent | 0–100 | Aggregate CPU utilisation |
| `cpu_core_count` | integer | count | ≥ 1 | Logical cores |
| `ram_total_bytes` | integer | bytes | > 0 | Physical RAM |
| `ram_used_bytes` | integer | bytes | 0–total | Used RAM |
| `swap_used_bytes` | integer | bytes | ≥ 0 | Swap in use |
| `load_avg_1m` | number | loadavg | ≥ 0 | 1-minute load average |
| `cpu_temp_celsius` | number | °C | ≥ 0 | CPU temp (absent if no sensor) |
| `os_name` | string enum | — | `Ubuntu` | OS name |
| `os_version` | string enum | — | `24.04` | OS version |
| `kernel_version` | string | — | `uname -r` release only | Kernel release string, image-pinned |
| `lxd_cluster_role` | string enum | — | `member` | LXD cluster role |

`kernel_version` is the `uname -r` release string only (e.g. `6.8.0-142-generic`),
image-pinned and identical across every node on the same bake. The full
`/proc/version` string (compiler, build host, build timestamp) is a host
fingerprint and is NOT sent.

### 4.2 Identifiers — C1 (governance.md §4.2, transformed)

These MUST arrive already transformed by the on-node redactor; the controller
re-verifies and rejects untransformed values (governance.md §9 hook 1).

| Field | Type | Transformation |
|-------|------|----------------|
| `hostname` | string | SHA-256 hash, hex, truncated to 16 chars + project_id-derived salt |
| `ip_local` | string | IPv4: last octet replaced with `.0/24`; IPv6: keep /48 prefix |
| `mac_address` | string | SHA-256 hash only |
| `project_id` | string (ULID) | Allowed verbatim (controller-issued, non-revealing) |

`ip_public` is DENIED by default (governance.md §4.2); it never appears in
telemetry. `lxd_cluster_member` is replaced with `node_id` before egress.

### 4.3 Per-device disk — C0 fields (governance.md §4.1)

`disk[]` — one entry per block device. `path` is the hardware device path (not a
customer data path).

| Field | Type | Unit | Range | Description |
|-------|------|------|-------|-------------|
| `path` | string | — | — | Block device, e.g. `/dev/sda` |
| `total_bytes` | integer | bytes | > 0 | Device size |
| `free_bytes` | integer | bytes | 0–total | Free space |
| `write_latency_ms` | number | ms | ≥ 0 | Write I/O latency (Scenario B signal) |
| `mount_ro` | boolean | — | — | Mount flipped read-only (disk failure) |

### 4.4 Per-interface network — C0 fields (governance.md §4.1)

`net[]` — one entry per interface.

| Field | Type | Unit | Range | Description |
|-------|------|------|-------|-------------|
| `iface` | string | — | — | Interface name, e.g. `eth0` |
| `rx_bytes` | integer | bytes | ≥ 0 | Receive counter |
| `tx_bytes` | integer | bytes | ≥ 0 | Transmit counter |
| `latency_ms` | number | ms | ≥ 0 | Round-trip to controller |

### 4.5 GPU — C0 fields (governance.md §4.1)

`gpu[]` — one entry per physical GPU. Demo has exactly 1 (node C). When no GPU is
present the array is empty (governance's `gpu_present` is derivable: `gpu[].length > 0`).

| Field | Type | Unit | Range | Description |
|-------|------|------|-------|-------------|
| `model` | string enum | — | `NVIDIA L4` etc. | Model; no serial |
| `util_pct` | number | percent | 0–100 | SM utilisation |
| `vram_total_bytes` | integer | bytes | > 0 | Total VRAM |
| `vram_used_bytes` | integer | bytes | 0–total | Used VRAM |
| `temp_celsius` | number | °C | ≥ 0 | GPU temperature |

### 4.6 Per-instance LXD metrics — C0 (extension)

`lxd_instances[]` — one entry per running instance, keyed by the controller-issued
`vm_id` (never the LXD instance name, which may carry customer hostname data).

The array is **omitted**, not emptied, when the node cannot read its own LXD
(no LXD socket, LXD not answering): "this node did not report an inventory" and
"this node runs nothing" are different facts, and an empty list silently claims
the second. An empty array is reported only when LXD answers with zero running
instances. Only instances in LXD's `Running` state are reported; a stopped
instance is not a remediation target and is not advertised.

`vm_id` is derived from the instance name, identically on both sides:

    vm_id = 26 Crockford base32 characters of SHA-256("termini-vm-id:" + name)

(i.e. the top 130 bits of the digest, MSB-first). It is deterministic and
one-way, so the name — which may carry customer data — never reaches the
controller, the model prompt or an audit row, and the controller can turn an
instance name it reads back over the node's LXD REST API
(`controller/app/operator/vmids.py`) into the same id, and vice versa. A reported
`vm_id` that matches no instance on the node being dialled is therefore, by
construction, not a target: there is no per-instance mapping table that can be
missing, and no id is ever invented. The node daemon
(`infra/bootstrap/node/termini-bootstrap.py`), the controller and the demo seeder
carry the same rule; `tests/test_node_instance_inventory.py` pins the literals so
a drift between the copies fails the build instead of silently making every
remediation unresolvable.

| Field | Type | Unit | Range | Description |
|-------|------|------|-------|-------------|
| `vm_id` | string (ULID) | — | — | Controller-issued VM id (C0); derived per the rule above |
| `state` | string enum | — | `running|stopped|error|…` | LXD instance state |
| `cpu_util_pct` | number | percent | 0–100 | Per-instance CPU, percent of node capacity. Omitted on the sample where no delta is available yet (the first sample after a node or instance start) rather than reported as 0 |
| `ram_used_bytes` | integer | bytes | ≥ 0 | Current VM RAM usage |
| `ram_limit_bytes` | integer | bytes | ≥ 0 | LXD's effective memory limit for the instance (`limits.memory`, or the node's total when the instance is unlimited) |
| `disk_used_bytes` | integer | bytes | ≥ 0 | Root volume used. 0 on a storage driver that does not track per-instance usage (the `dir` driver) — the same number `lxc list` reports |
| `state_age_sec` | integer | s | ≥ 0 | Seconds the instance has been in its current state (LXD `state.started_at`); **omitted** when LXD cannot say |
| `restart_count` | integer | count | ≥ 0 | Restarts of this instance **observed by the reporting daemon** since that daemon started (LXD exposes no restart counter of its own); a monotonic per-daemon count, reset by a node reboot |

`last_exit_code` is deliberately **not** a §4.6 field: LXD does not expose an
exit status for an instance (`/1.0/instances/<name>` has no equivalent of
systemd's `ExecMainStatus`), so a field here could only ever be invented. The
per-unit exit status lives in §4.8, where systemd actually reports it.

`state_age_sec` is **structurally absent** on an LXD that does not expose an
instance start time, and it is never faked in its place. Measured against the
snap-packaged LXD 5.x this fleet runs (2026-10-16): neither
`GET /1.0/instances?recursion=2` nor `GET /1.0/instances/<name>/state` returns
`started_at`, so the field is omitted from **every** `lxd_instances[]` entry and a
viewer sees it absent — not `0` — on such a node. That is the same "omit, never
invent" rule as `last_exit_code` above and the §4.11 journal counts. Age is *not*
derived from the instance object's `created_at`/`last_used_at`: that is
creation/last-use age, not state age, and reporting it under this name would make
the field a lie.

### 4.7 Inference (llama.cpp) — C0 (extension)

Present only on the node hosting the inference **service** (Node C, host-exposed
per D5). `model` is the currently-served alias: today exactly `gemma` (Gemma 3 4B
IT, resident). The image's catalog distills (`deepseek-flash`, `deepseek-v4-pro`)
remain baked and stay legal in this field, but they are no longer wired for the
agent or the operator; a node reporting one of them is reporting a catalog pin,
not a served alias.

| Field | Type | Unit | Range | Description |
|-------|------|------|-------|-------------|
| `server_up` | boolean | — | — | llama.cpp HTTP endpoint reachable |
| `model` | string enum | — | `gemma|deepseek-flash|deepseek-v4-pro` | Active model (served alias; catalog pins remain legal) |
| `active_requests` | integer | count | ≥ 0 | In-flight requests |
| `queue_depth` | integer | count | ≥ 0 | Waiting requests |
| `tokens_per_s` | number | tok/s | ≥ 0 | Generation throughput |
| `vram_used_bytes` | integer | bytes | ≥ 0 | Model VRAM footprint |

### 4.8 Service status — C0 (extension)

`services[]` — systemd unit state. `name` is a fixed allowlist; anything else is
dropped at ingest. The array is emitted on **every** sample (a missing list renders
as "no services to show", which is indistinguishable from a node that reports
none): `termini-bootstrap` is the unit producing the sample, `lxd` and `sshd` are
reported from their systemd state on every node, and `llama-cpp` appears on the
node that hosts the inference front door and nowhere else. A service the node does
not have is omitted, never reported as `stopped`.

Allowlist: `termini-bootstrap`, `lxd`, `sshd`, `llama-cpp`.

| Field | Type | Range | Description |
|-------|------|-------|-------------|
| `name` | string enum | see allowlist | Unit name |
| `status` | string enum | `running|failed|stopped|exited` | systemd active state |
| `state_age_sec` | integer | ≥ 0 | Seconds since the unit last entered this active state (`ActiveEnterTimestamp`); omitted when systemd does not report it |
| `restart_count` | integer | ≥ 0 | systemd `NRestarts` — how many times the unit has been restarted; omitted when systemd cannot say (a node without a readable systemd must not report `0`) |
| `last_exit_code` | integer | `-1` … 255 | `ExecMainStatus` of the unit's most recent run; omitted when the unit has never exited |
| `journal_errors` | integer | ≥ 0 | Count of journal entries at priority `err`+ for the unit in the last `JOURNAL_WINDOW_S` (300 s); omitted when the journal is unreadable — never `0`-by-default (§4.11) |
| `journal_warnings` | integer | ≥ 0 | Count of journal entries at priority `warning` for the unit in the same window; omitted under the same rule (§4.11) |

The operational fields are what turn `services[]` from a boolean into evidence:
`status: failed` plus `restart_count: 2` plus `last_exit_code: 137` plus
`state_age_sec: 4` is a crash loop, and it is an incident an operator can act on
without a single log line.

### 4.9 Extension registration note

§4.6–§4.8 are C0 hardware/OS metrics required by the AI operator (per-VM instance
state, llama.cpp health) and the charts. They are **now registered** in
`governance.md` §4.1.1 as C0 allowlist groups (deny-by-default review complete,
integration pass t_5fb81d14). The node redactor treats these exact keys as C0
allowlist entries. No field in this document is C2 or carries customer data.

The §4.6/§4.8 operational fields and the two new groups below (`processes[]`
§4.10, journal counts §4.11) were added under the same deny-by-default review
(t_957956e5) and are registered in `governance.md` §4.1.1 alongside the earlier
ones. Every field is C0; no field of any group is C2, and no group introduces a
free-form string other than the bounded `processes[].name` (§4.10).

---

### 4.10 `processes[]` — top-N process metrics — C0 (extension)

`processes[]` — what is running *inside* the node, at the metric level only. It
is what lets an incident be explained from telemetry alone, and it is
deliberately **not** a process listing in the `ps`/`top` sense.

**Scope decision (recorded, t_957956e5): metrics-only.** An entry carries the
kernel's own task name and its counters and nothing else:

- `name` is the kernel task name (`/proc/<pid>/comm`), at most 15 printable
  ASCII characters — never `argv`, never `cmdline`, never `/proc/<pid>/exe`,
  never the environment. It has no arguments, no path and no user field, and at
  ≤15 characters it cannot carry a document, a URL, a prompt or a key.
- `state` is a closed enum; the only other string in an entry is `name`.

`argv`, `cmdline` (and `argv[0]` with arguments), `environ`, `exe`, `cwd`, open
file paths, usernames and application log lines stay **C2** (§6) and are refused
at ingest: `argv`/`cmdline`/`env` were already fail-closed by field name, and
`exe`/`cwd` were added to the same denylist with this change (t_957956e5) so a
process *path* is rejected loudly instead of being silently stripped.
Adding `processes[]` therefore does **not** relax the contract's C2 denial and
needs no recorded exception to it.

The array is **omitted**, not emptied, when the node cannot read `/proc` for
itself — the same rule as `lxd_instances[]` (§4.6): "this node did not report a
process table" and "this node runs nothing" are different facts.

**Bound (volume/retention decision: recorded, t_957956e5; amended 2026-10-16 by the
owner's note, t_74b5cce0): top-10 per sample, ranked by `ram_used_bytes`
descending**, then `cpu_util_pct` descending, then `pid` ascending — a total
order, so two nodes sampling the same host agree on the set. The array rides the
**existing** telemetry cadence (§1): no new interval, no additional egress, one
`/proc` sweep per telemetry sample.

| Field | Type | Unit | Range | Description |
|-------|------|------|-------|-------------|
| `pid` | integer | — | ≥ 1 | Process id — ties the sample to live `ps` output on the node |
| `name` | string | — | pattern `^[ -~]{1,15}$` | Kernel task name (`comm`): printable ASCII, ≤15 chars, no arguments |
| `state` | string enum | — | `running\|sleeping\|disk_sleep\|zombie\|stopped\|tracing\|dead` | Kernel process state (`/proc/<pid>/stat` field 3) |
| `cpu_util_pct` | number | percent | 0–100 | CPU consumed by this process over the sample delta, as percent of the node's total capacity |
| `ram_used_bytes` | integer | bytes | ≥ 0 | Resident set size (resident pages × page size) |
| `threads` | integer | count | ≥ 1 | Thread count (`/proc/<pid>/status` `Threads`) |
| `age_sec` | integer | s | ≥ 0 | Seconds since process start (`/proc/<pid>/stat` field 22 vs `/proc/uptime`) |

`cpu_util_pct` is omitted on the first sample after a node or process start (no
delta available yet) rather than reported as `0` — the same convention as
`lxd_instances[].cpu_util_pct` (§4.6).

**Recorded objection (2026-10-16, decided: ship RSS-primary as stated).** An
RSS-ranked window is blind to a CPU-bound runaway with a small resident set, and
this sample is the only process evidence an incident has. The demo's frozen rules
are OOM / disk-fail / disk-degrade / latency, so RSS-primary is right for
everything the demo actually fires; the residual risk is a day-2 CPU rule, not a
demo one, and it is recorded here rather than hidden. If such a rule arrives, the
cheap fix is to keep the window RSS-ranked **and** always include the single
highest-`cpu_util_pct` process; that changes the set (and therefore this
paragraph), so it is a named follow-up, not an unrecorded drift.

---

### 4.11 Journal counts — counts, not lines — C0 (extension)

**Decision (recorded, t_957956e5): counts of log *entries* ARE permissible where
raw log lines are not.** An error/warning *count* over a fixed window is an
integer describing a unit's behaviour — an aggregate, not a log line — and
carries none of the message, timestamp, identifier or payload of any one entry.
It is the smallest thing that makes "the proxy restarted twice and logged 40
errors" visible without shipping a log.

It is realised as two bounded integers on `services[]` (§4.8) and never as text:

| Field | Type | Unit | Range | Description |
|-------|------|------|-------|-------------|
| `journal_errors` | integer | count | ≥ 0 | Entries at syslog priority `err` or worse for that unit in the last `JOURNAL_WINDOW_S` (**300 s**) |
| `journal_warnings` | integer | count | ≥ 0 | Entries at priority `warning` for that unit in the same window |

Both are **omitted** (never `0`) when the node cannot read its own journal, so
"no errors" is never invented from "could not ask". The journal *messages* remain
C2: `log`, `logs`, `log_line`, `syslog`, `message`, `msg` and the rest are on the
field-name denylist (`governance.md` §4.3), so no line can ride along inside a
count field — and the contract's "Never collected" list in §6 is unchanged.

---

## 5. Trigger linkage (remediation traceability)

The AI operator's remediation loop consumes this stream. When a threshold rule
fires and the operator issues a remediation command, the controller records the
trigger with the command so every autonomous action is traceable:

```json
{
  "trigger": {
    "sample_seq": 1050,
    "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
    "metric": "ram_used_bytes",
    "value": 32641751449,
    "threshold": ">= 95% of ram_total_bytes",
    "rule_id": "oom-guard-01",
    "fired_at": "2026-10-04T07:49:05Z"
  },
  "action": "instance.restart",
  "command_id": "01M4349RPY26W8Q453PZYK05CB"
}
```

This linkage is written into the guardrail audit record (`actor: ai_operator`,
`controller-api.md` §5.4) so an auditor can reconstruct exactly which telemetry
sample justified which autonomous change.

Threshold rules (demo, frozen) — the **detection layer**:

| rule_id | metric | condition | severity |
|---------|--------|-----------|----------|
| `oom-guard-01` | `ram_used_bytes` | ≥ 95% of `ram_total_bytes` for 2 consecutive samples | critical |
| `disk-fail-01` | `disk[].mount_ro` | any `true` | critical |
| `disk-degrade-01` | `disk[].write_latency_ms` | ≥ 50 for 5 consecutive samples | degraded |
| `gpu-oos-01` | `inference.server_up` | `false` for 3 consecutive samples | degraded |

Firing a rule is a **wake-up signal, not a decision**. What is normative is that these
conditions raise the trigger and that the trigger is recorded with the action (as
above). What is *no longer* normative is that a given rule maps to a given action: the
action is chosen by the operator's decision step, which may be a declarative playbook or
a model (`governance.md` §7.3). Either way the chosen action must satisfy that section's
constraint set, and the audit row records which decider chose it (`decision_source`).

The action space the decision step selects from is the LXD REST remediation kinds the
executor issues (`controller/app/operator/executor.py`: `instance.restart`,
`instance.stop`, `instance.delete`, `image.prune`), plus surfacing the fault with no
autonomous action. There is no `vm.resize`/`node.drain` remediation in the loop;
`node.drain`/`node.reboot` exist only as human-initiated node actions
(`controller-api.md` §4.2 / `dashboard.md` §4).

`gpu-oos-01` is the canonical case where the right answer is *no action*: inference down
has no safe autonomous remediation, so the decision step must surface it. Note precisely
what enforces that — the allowlist admits `instance.restart`, so a decider proposing a
restart here is *inside* the allowlist and outside **policy**. Nothing in the gate
rejects it. A verifier asserting this rule must therefore check the outcome (alert
raised, no command issued), not assume the gate caught it.

Detection of `gpu-oos-01` is currently realised outside the operator loop: the
`inference.server_up` fault is surfaced by the controller's health layer
(`controller/app/health.py`), and `detect.py` `RULE_IDS` implements only
`oom-guard-01`, `disk-fail-01`, `disk-degrade-01`, `latency-01`. Wiring `gpu-oos-01`
as a first-class operator trigger — firing the wake-up signal and recording a
*surface only, no autonomous action* decision — is a day-2 item, not part of the
frozen build.

---

## 6. Egress boundary (summary)

Allowed to leave the premises: every field in §2 and §4, after the on-node
redactor transforms C1 fields. These are all C0/C1 hardware/OS metrics with no
customer content.

Never collected, and rejected at ingest if present (C2): application log lines,
customer documents, user prompts, process `argv`, environment variables, file
contents, raw MAC addresses, full IP addresses, serial numbers, asset tags, SSH
keys, and any free-form string not enumerated above. The redactor is the single
egress gate (governance.md §5); the controller additionally validates inbound
telemetry against this allowlist (governance.md §9 `governanceGate`) and rejects
discrepancies with `422 GOVERNANCE_REJECT`.

`processes[]` (§4.10) and the journal counts (§4.11) do not narrow this list.
A process **name** (the kernel `comm`, ≤15 printable ASCII chars, no arguments,
no path) and an error/warning **count** are aggregates over the machine's own
behaviour; a process command line, its environment, its open files and the
journal *messages* behind a count remain C2 and are rejected exactly as before.
The denylist is enforced by field *name* at every nesting depth, so a `cmdline`
or `message` nested inside a `processes[]` entry is rejected the same way a
top-level one is — and that is pinned by test, not by inspection.

---

## 7. Validation rules at ingest (fail-closed, whole-batch)

The controller applies these, in order, to the batch (governance.md §9 hooks):

1. **C2 scan** (field-name denylist + PII signatures: email, phone, raw IPv4/IPv6,
   raw MAC, private key, cloud credential, card number, UUID, base64 blob) → any
   hit rejects the **whole batch** with `422 GOVERNANCE_REJECT`.
2. **C1 transform check** (untransformed `hostname`/`ip_local`/`mac_address`, or
   `ip_public`) → `422 GOVERNANCE_REJECT`.
3. **`assertRedactorVersion(min)`** — `_redaction.version` below the controller
   minimum → `422 GOVERNANCE_REJECT`.
4. **Envelope/sample schema** — missing required fields, type/range/enum
   violations, or non-monotonic `seq` → reject the **whole batch** with
   `422 INVALID_ARGUMENT` and store nothing. Unknown fields are stripped
   (deny-by-default), never persisted.
5. **`node_id`** must match the path/token → `403` otherwise (batch-level).

Because the demo runs at 1 s cadence with bounded batches, whole-batch fail-closed
is the safer choice: no malformed data is ever persisted, and the next valid batch
arrives within one interval. Per-sample `seq` dedup (out-of-order/duplicate) is
applied within an accepted batch and reported as `rejected` in the `202` response.

The node-side daemon performs the same checks before sending (fail-closed: a
metric it cannot classify as C0/C1 is dropped locally, never sent).

No field in this contract is a TBD.
