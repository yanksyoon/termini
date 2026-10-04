# Telemetry Schema — Termini Controller (enforced at ingest)

Status: published, enforced. Owner: controller team.

This document is the machine-aligned schema the controller enforces on
`POST /v1/nodes/{node_id}/telemetry`. It is derived from — and stays consistent
with — the frozen field contract in `docs/contracts/node-telemetry.md` §2–§4
(canonical field names, units, ranges, and C0/C1/C2 classes) and the governance
rules in `docs/contracts/governance.md` §4–§6. The authoritative allowlist lives
in `controller/app/fields.py`; this document is written from that same table, so
the published schema and the enforced schema cannot drift. An acceptance check
(`scripts/acceptance.py`) verifies the two agree on every field name.

## 1. Endpoint and transport

| Property | Value |
|----------|-------|
| Endpoint | `POST /v1/nodes/{node_id}/telemetry` |
| Auth     | `Authorization: Bearer <node_token>` (node-scoped; cross-node → 403) |
| Content-Type | `application/json` |
| Max body | 1 MiB |
| Cadence  | one batch per `telemetry_interval_ms` (default 1000 ms = 1 s) |
| Batch size | 1..200 samples |
| Success  | `202 Accepted` → `{ "accepted": N, "rejected": M, "backpressure": "none" }` |

The envelope `node_id` must match the path `{node_id}` (403 otherwise). A sample
whose `seq` is not strictly greater than the last stored `seq` for that node is
dropped as a duplicate/out-of-order retry (counted in `rejected`), never stored.

## 2. Batch envelope

Every batch is a single JSON object:

```json
{
  "_redaction": { "version": "redactor-config v3", "config_hash": "3f2a1b…", "result": "ok" },
  "call_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C5J",
  "node_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C1E",
  "schema_version": 1,
  "governance_chain_tip": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C6K",
  "samples": [ { "...": "one Sample object per §3" } ]
}
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `_redaction.version` | string | yes | `redactor-config vN`; `N` must be >= controller minimum (default 3) |
| `_redaction.config_hash` | string | yes | redactor config hash |
| `_redaction.result` | string enum | yes | must be `"ok"` (a `blocked` payload never egresses) |
| `call_id` | string (ULID) | yes | unique per batch |
| `node_id` | string (ULID) | yes | must match path param |
| `schema_version` | integer | yes | always `1` |
| `governance_chain_tip` | string | no | hash-chain tip for reconciliation |
| `samples` | array | yes | 1..200 Sample objects |

Identifiers are ULIDs: 26 chars of Crockford base32 (uppercase,
`0-9A-HJKMNP-TV-Z`, no I/L/O/U). Timestamps are RFC 3339 UTC with millisecond
precision and are stored **verbatim** (never normalized).

## 3. Sample object

One `Sample` is a flat, timestamped object. `ts` and `seq` are required; at
least one metric field is required; all other fields are optional. Unknown
fields are stripped (deny-by-default).

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
  "project_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C0D",
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
    { "vm_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C4H", "state": "running",
      "cpu_util_pct": 12.0, "ram_used_bytes": 3328599654,
      "ram_limit_bytes": 4294967296, "disk_used_bytes": 2362232012 }
  ],
  "inference": {
    "server_up": true, "model": "deepseek-flash", "active_requests": 3,
    "queue_depth": 0, "tokens_per_s": 48.0, "vram_used_bytes": 12884901888
  },
  "services": [
    { "name": "termini-bootstrap", "status": "running" },
    { "name": "lxd", "status": "running" },
    { "name": "llama-cpp", "status": "running" }
  ]
}
```

### 3.1 Host metrics (C0)

| Field | Type | Unit | Range |
|-------|------|------|-------|
| `ts` | string RFC3339 | — | — |
| `seq` | integer | — | >= 0 |
| `uptime_sec` | integer | seconds | >= 0 |
| `cpu_util_pct` | number | percent | 0–100 |
| `cpu_core_count` | integer | count | >= 1 |
| `ram_total_bytes` | integer | bytes | >= 1 |
| `ram_used_bytes` | integer | bytes | >= 0 |
| `swap_used_bytes` | integer | bytes | >= 0 |
| `load_avg_1m` | number | loadavg | >= 0 |
| `cpu_temp_celsius` | number | celsius | >= 0 |
| `os_name` | string enum | — | `Ubuntu` |
| `os_version` | string enum | — | `24.04` |
| `kernel_version` | string | — | sanitized |
| `lxd_cluster_role` | string enum | — | `member` |

### 3.2 Identifiers (C1 — must arrive already transformed)

| Field | Type | Transformation enforced |
|-------|------|-------------------------|
| `hostname` | string | SHA-256 hash, hex, truncated to 16 chars |
| `ip_local` | string | IPv4: last octet → `.0/24`; IPv6: `/48` prefix |
| `mac_address` | string | SHA-256 hash, 16 hex chars |
| `project_id` | string (ULID) | verbatim (controller-issued) |

`ip_public` is denied. An untransformed `hostname`/`ip_local`/`mac_address`
rejects the batch (`422 GOVERNANCE_REJECT`).

### 3.3 Per-device disk — `disk[]` (C0)

| Field | Type | Unit | Range |
|-------|------|------|-------|
| `path` | string | — | — |
| `total_bytes` | integer | bytes | >= 1 |
| `free_bytes` | integer | bytes | >= 0 |
| `write_latency_ms` | number | ms | >= 0 |
| `mount_ro` | boolean | — | — |

### 3.4 Per-interface network — `net[]` (C0)

| Field | Type | Unit | Range |
|-------|------|------|-------|
| `iface` | string | — | — |
| `rx_bytes` | integer | bytes | >= 0 |
| `tx_bytes` | integer | bytes | >= 0 |
| `latency_ms` | number | ms | >= 0 |

### 3.5 GPU — `gpu[]` (C0; empty when no GPU)

| Field | Type | Unit | Range |
|-------|------|------|-------|
| `model` | string | — | — |
| `util_pct` | number | percent | 0–100 |
| `vram_total_bytes` | integer | bytes | >= 1 |
| `vram_used_bytes` | integer | bytes | >= 0 |
| `temp_celsius` | number | celsius | >= 0 |

### 3.6 LXD instances — `lxd_instances[]` (C0)

| Field | Type | Unit | Range |
|-------|------|------|-------|
| `vm_id` | string (ULID) | — | — |
| `state` | string enum | — | `running\|stopped\|error\|starting\|paused\|frozen\|migrating\|idle` |
| `cpu_util_pct` | number | percent | 0–100 |
| `ram_used_bytes` | integer | bytes | >= 0 |
| `ram_limit_bytes` | integer | bytes | >= 0 |
| `disk_used_bytes` | integer | bytes | >= 0 |

### 3.7 Inference (llama.cpp) — `inference` object (C0)

| Field | Type | Unit | Range |
|-------|------|------|-------|
| `server_up` | boolean | — | — |
| `model` | string enum | — | `deepseek-flash\|deepseek-v4-pro` |
| `active_requests` | integer | count | >= 0 |
| `queue_depth` | integer | count | >= 0 |
| `tokens_per_s` | number | tok/s | >= 0 |
| `vram_used_bytes` | integer | bytes | >= 0 |

### 3.8 Service status — `services[]` (C0)

| Field | Type | Range |
|-------|------|-------|
| `name` | string enum | `termini-bootstrap\|lxd\|sshd\|llama-cpp` |
| `status` | string enum | `running\|failed\|stopped\|exited` |

## 4. Validation behavior (fail-closed)

The controller validates every batch in this order:

1. **C2 scan** (field-name denylist + PII signatures: email, phone, raw IPv4/IPv6,
   raw MAC, private key, cloud credential, card number, UUID, base64 blob) →
   any hit rejects the **whole batch** with `422 GOVERNANCE_REJECT`.
2. **C1 transform check** (untransformed `hostname`/`ip_local`/`mac_address`, or
   `ip_public`) → `422 GOVERNANCE_REJECT`.
3. **`assertRedactorVersion(min)`** — `_redaction.version` below the controller
   minimum → `422 GOVERNANCE_REJECT`.
4. **Envelope/sample schema** — missing required fields, type/range/enum
   violations, `seq` non-monotonic are rejected; a malformed batch returns
   `422 INVALID_ARGUMENT` (error envelope per `controller-api.md` §3) and stores
   nothing. Unknown fields are stripped (deny-by-default), never persisted.

The demo controller uses strict whole-batch validation: a batch containing any
malformed sample is rejected in full and nothing from it is stored. (The frozen
contract permits per-sample `rejected` counting for partial acceptance; the demo
opts for fail-closed all-or-nothing to guarantee no malformed data is ever
persisted.)

Units are bytes (never GiB) and percent, per `governance.md`. No field carries
application logs, customer payloads, or credentials (C2 — never egresses).
