#!/usr/bin/env python3
"""Seed a live W5 demo controller with nodes + a 1 s telemetry heartbeat.

Registers a compute node and an inference node with the controller's single-use
bootstrap tokens, then streams telemetry so the dashboard shows live health and
the AI-operator loop has fresh samples to act on. The inference node is driven
through a memory-pressure (OOM) fault window so the operator emits a steady
stream of log lines — exactly what the dashboard's *live AI-operator log* panel
and the ``/logs`` SSE surface consume. The compute node reports healthy
telemetry. The demo fault library offers only `oom` and `disk-fail`; the
`disk-degrade` and `latency` detector rules remain implemented in the controller
(`operator/detect.py`) but the demo can no longer inject them. Pass
``--no-faults`` (alias ``--healthy``) to hold a fully healthy baseline: both
synthetic nodes keep reporting HEALTHY telemetry at the normal cadence (the
fleet stays visible and the header pills stay green) with no fault window
driven, so the operator can inject exactly one fault from the console.

Run until interrupted (or ``--duration`` seconds), so start it next to the
controller and the composed console:

    python3 console/seed_demo.py --controller http://127.0.0.1:8085 \\
        --token <project_token> --bootstrap demo-boot-a,demo-boot-c

**These nodes are fabricated.** They are registered through the same public
endpoint, with the same single-use bootstrap tokens, as real hardware — so every
registration body here declares ``"synthetic": true``. That flag is what lets the
controller (a) refuse to place an allocation on these rows while any real node is
registered, and (b) label them on the dashboard. Without it a fabricated row is
indistinguishable from a real node and `auto` placement lands agents on a node
that does not exist.

It is deliberately stdlib-only. Nothing here is required for acceptance — it is
the "make the demo look alive" helper, and a dashboard fallback for a demo with
no real node registered.

**Failure contract.** A telemetry POST that times out is RETRIED and never kills
the ticker thread; a ticker that dies anyway is respawned by the watchdog in
`main`, and a ticker that is alive but silent is reported loudly on stderr. The
reason for the care: this process is the only thing keeping the synthetic nodes
ONLINE, and a dead ticker thread is invisible from outside — `main` keeps
sleeping, `systemctl is-active termini-seeder` keeps saying ACTIVE, and the demo
freezes silently (nodes flip OFFLINE ~45 s later). See `NodeTicker` and
`watchdog_pass`.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

L4_24GB = 24 * 1024 ** 3

# Deadline for one HTTP call. Registration/minting keep the historical 15 s: they
# are one-shot and a failure there is fatal *on purpose* (`register` exits).
HTTP_TIMEOUT_S = 15.0
# Telemetry runs on a 1 s cadence, so 15 s of silence already means "the controller
# is busy", not "this sample is precious". It is RETRIED (never fatal) — see
# NodeTicker.send.
TELEMETRY_TIMEOUT_S = 15.0
# Warn out loud when no sample has been accepted for this long. The controller
# marks a node `heartbeat_late` at 15 s and OFFLINE at 45 s (controller/app/
# health.py: DEGRADED_AFTER_S / OFFLINE_AFTER_S), so a 20 s warning lands while the
# dashboard still looks merely degraded.
DEFAULT_STALL_WARN_S = 20.0

# Statuses the controller uses to accept a telemetry batch.
TELEMETRY_ACCEPTED = (200, 202)


class TransportError(Exception):
    """A request never got an HTTP ANSWER, so its content was never seen.

    ``kind`` is ``"timeout"`` when the request was accepted but nothing came back
    inside the deadline, and ``"unreachable"`` when the connection itself failed
    (controller down or restarting, mid-flight reset, bad status line). Both are
    RETRYABLE and neither says anything about the sample, which is why the ticker
    retries them while a 4xx is not retried.

    Why this type exists at all: `urlopen`'s raw exceptions used to escape
    `_request` and land in `NodeTicker.run`, where nothing caught them. The thread
    ended, `main` kept sleeping, systemd kept reporting the unit ACTIVE, and
    telemetry stopped for good — the live `TimeoutError: timed out` freeze. A
    distinct type makes "retry me" distinguishable from "your sample is wrong".
    """

    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


# Telemetry gpu[] for the GPU node (node-telemetry.md §4.5). The card reported
# here must match the one declared in the registration hardware facts below.
# A node whose role is `inference` but which omits `gpu[]` derives
# `gpu_ok = false` (dashboard.md §1.3: "false if gpu[] empty") -> reason
# `gpu_unavailable` -> a permanently degraded card, so the GPU node must report
# it on every sample. Compute nodes have no GPU and must NOT send this array.
GPU_TELEMETRY = [{
    "model": "NVIDIA L4",
    "util_pct": 38.0,
    "vram_total_bytes": L4_24GB,
    "vram_used_bytes": 12884901888,
    "temp_celsius": 58.0,
}]

# Crockford base32 ULID (48-bit ms timestamp + 80-bit randomness) — the id class
# the controller validates (`controller/app/ulid.py`). Kept local so this script
# stays stdlib-only and independent of the controller package.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    val = (ts << 80) | int.from_bytes(os.urandom(10), "big")
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[val & 0x1F])
        val >>= 5
    return "".join(reversed(out))


def _request(base: str, method: str, path: str, body=None, token: str | None = None,
             timeout: float = HTTP_TIMEOUT_S):
    """One HTTP call. Raises `TransportError` when no answer arrives.

    Only HTTP statuses are returned as data; a missing answer is an exception the
    caller can retry, so a network hiccup can never be mistaken for a rejection.
    """
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        # Closed explicitly: a seeder that meets a controller returning 5xx for
        # minutes would otherwise leak one socket per rejected sample until GC.
        try:
            raw = e.read()
        finally:
            e.close()
        try:
            return e.code, (json.loads(raw) if raw else None)
        except json.JSONDecodeError:
            return e.code, raw.decode(errors="replace")
    except urllib.error.URLError as e:
        # A read that times out mid-response arrives here wrapped as often as it
        # arrives bare, so `reason` decides the kind rather than the exception type.
        kind = "timeout" if isinstance(e.reason, TimeoutError) else "unreachable"
        raise TransportError(kind, f"{method} {path}: {e.reason}") from e
    except (OSError, http.client.HTTPException) as e:
        # Bare TimeoutError/socket.timeout (the live freeze: "TimeoutError: timed
        # out"), ConnectionResetError/RemoteDisconnected, BadStatusLine — all mean
        # "no answer". Note this branch must be last: HTTPError and URLError are
        # both OSError subclasses.
        kind = "timeout" if isinstance(e, TimeoutError) else "unreachable"
        raise TransportError(kind, f"{method} {path}: {type(e).__name__}: {e}") from e


def registration_body(machine_id: str, gpus: list) -> dict:
    """The seeder's registration body.

    `"synthetic": true` is the ONLY thing that distinguishes these rows from real
    hardware, and it is mandatory here: the seeder registers through the same
    public `POST /v1/nodes/register` endpoint with the same single-use bootstrap
    tokens a node uses, so without the marker the controller cannot tell a
    fabricated node from a real one — and `auto` target-node resolution, having
    only reported capacity to go on, picked this node's inflated 64 GiB facts over
    the real GCE node's real ones and allocated agents onto a node that does not
    exist. The controller now prefers / requires real nodes whenever any real node
    is registered (controller-api.md §5.2 step 5).
    """
    return {
        "schema_version": "1.0",
        "synthetic": True,
        "machine": {
            "machine_id": machine_id,
            "hostname": machine_id,
            "cpu": {"model": "AMD EPYC 7B13", "cores": 8, "threads": 16},
            "memory": {"total_bytes": 68719476736},
            "disks": [{"device": "/dev/sda", "size_bytes": 274877906944}],
            "gpus": gpus,
        },
    }


def register(base: str, boot_token: str, machine_id: str, gpus: list) -> dict:
    body = registration_body(machine_id, gpus)
    try:
        st, r = _request(base, "POST", "/v1/nodes/register", body, boot_token)
    except TransportError as e:
        # Registration is one-shot and FATAL on purpose: without a node row there is
        # nothing to stream telemetry for, and the bootstrap token is single-use so
        # a retry would have to mint a new one. Fail loudly instead of half-starting.
        raise SystemExit(f"register {machine_id} failed: {e}") from e
    if st not in (200, 201) or not isinstance(r, dict):
        raise SystemExit(f"register {machine_id} failed: {st} {r}")
    return r


def mint_bootstrap_tokens(base: str, project_token: str, count: int) -> list[str]:
    """Ask the controller for `count` FRESH single-use bootstrap tokens.

    A bootstrap token is burned by the first registration, so the seeder cannot
    simply reuse the tokens it was started with: the second run (or the first run
    after any restart) would authenticate with an already-used token and fail
    `401 bootstrap token already used`. That is what made the seeder need
    hand-minted tokens written into its systemd unit — and a recording landmine
    when they went stale.

    `POST /v1/bootstrap-tokens` mints new UNUSED single-use tokens on demand. It
    is project-token-authenticated and loopback-only (the seeder runs beside the
    controller and talks to `127.0.0.1`). The tokens it returns are ordinary
    single-use rows: the registration path still burns them and a replayed token
    is still rejected, so the security property is unchanged.
    """
    st, r = _request(base, "POST", "/v1/bootstrap-tokens", {"count": count}, project_token)
    if st not in (200, 201) or not isinstance(r, dict):
        raise SystemExit(f"bootstrap-token mint failed: {st} {r}")
    tokens = r.get("tokens")
    if not isinstance(tokens, list) or len(tokens) < count:
        raise SystemExit(f"bootstrap-token mint returned no tokens: {r}")
    return [t for t in tokens if isinstance(t, str) and t]


# Stable synthetic LXD instance id per node.
#
# ``lxd_instances[]`` is a contract field (node-telemetry.md 4.6) and it is the
# ONLY source the operator's guardrail G5 has for resolving a remediation
# target. With the list empty, no model-chosen action can ever resolve, so every
# decision falls back with ``fallback_reason=invalid_target`` — the model is
# consulted and then always discarded. Emitting it makes the demo exercise the
# model-driven path for real.
#
# WHO the entry names is what decides whether the demo is honest, though. The
# guardrail resolves the entry's ``vm_id`` to the LXD instance the executor will
# act on, so a vm_id that does not belong to an instance on the node being
# dialled produces a decision that can only end in a refusal (and, before that
# guard existed, in `HTTP 404 Instance not found`). Two modes:
#
#   * ``--lxd-instance NAME[,NAME...]`` (recommended, and what the demo units
#     pass): advertise the controller-issued ``vm_id`` of a REAL instance on the
#     node the executor dials, derived by ``vm_id_for_instance`` exactly as the
#     node agent and the controller do. The action then lands on a real
#     instance — a simulated FAULT over a real TARGET, which is the demo we can
#     defend.
#   * ``--synthetic-instance-ids``: advertise an invented id (the historical
#     behaviour, kept only to demonstrate the fail-closed guard: the operator
#     logs `target_missing` and the client UI shows the reason instead of a raw
#     404). Never use it for a demo run.
_INSTANCE_VM_ID: dict[str, str] = {}


def initial_seq(now: float | None = None) -> int:
    """Starting sample sequence for a seeder run.

    MUST be monotonic across restarts. The controller discards samples whose `seq` is
    not greater than the highest it already stored for that node, so a wrapping value
    (the previous `int(time.time()) % 100000`) makes a restarted seeder produce seq
    numbers BELOW the previous run's — and every sample is dropped without an error.
    The unwrapped epoch cannot wrap on any realistic timescale.
    """
    return int(time.time() if now is None else now)


def instance_vm_id(node_id: str) -> str:
    if node_id not in _INSTANCE_VM_ID:
        digest = hashlib.sha256(("vm:" + node_id).encode()).hexdigest().upper()
        _INSTANCE_VM_ID[node_id] = "01M4N" + digest[:21]
    return _INSTANCE_VM_ID[node_id]


# Crockford base32 — the alphabet the controller's ULID validator accepts.
_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_VM_ID_PREFIX = "termini-vm-id:"


def vm_id_for_instance(name: str) -> str:
    """The controller-issued `vm_id` for LXD instance `name`.

    Byte-for-byte the same derivation as `infra/bootstrap/node/termini-bootstrap.py`
    (the node that reports its real inventory) and
    `controller/app/operator/vmids.py` (the controller that resolves a reported
    vm_id back to the instance). The seeder must use it too, or it advertises a
    target the guardrail cannot resolve and the executor will (correctly) refuse.
    `tests/test_vm_id_derivation.py` fails the build if the three ever drift.
    """
    digest = hashlib.sha256((_VM_ID_PREFIX + str(name)).encode("utf-8")).digest()
    value = int.from_bytes(digest[:17], "big") >> 6
    out = []
    for _ in range(26):
        out.append(_CROCKFORD_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def advertised_vm_ids(node_id: str, instance_names: list[str],
                      synthetic: bool) -> list[str]:
    """The `vm_id`s this node advertises in `lxd_instances[]`.

    `instance_names` are real LXD instance names on the node the executor dials
    (see the block above); their derived ids are what the controller resolves back
    to those names. `synthetic` restores the historical invented id.
    """
    if instance_names and not synthetic:
        return [vm_id_for_instance(n) for n in instance_names]
    return [instance_vm_id(node_id)]


# --- §4.12–§4.14 synthetic telemetry ----------------------------------------
# The metric-expansion groups the node collector now emits from real /proc,/sys,
# cgroup and LXD sources (PR #190). The seeder synthesises its own samples, so
# it must carry the SAME groups with values that tell the SAME fault story the
# existing RAM/disk windows already inject — otherwise the dashboard charts and
# the AI diagnosis see no `pressure{}` and the demo's answer is worth nothing.
# Values are fixed per fault window (deterministic, never random noise): the
# seeder drives faults as on/off windows, so the synthetic evidence follows the
# same on/off shape.

# The fault window a ticker drives. `None` is the healthy half of the period.
# Only the OOM (memory-pressure) window remains in the demo fault library;
# `disk-degrade` and `latency` are no longer injectable (their detector rules
# stay implemented in `controller/app/operator/detect.py`).
FAULT_MEMORY = "memory"   # memory-pressure (inference node): reclaim + OOM

# PSI pressure (node-telemetry.md §4.12): percent of time tasks stalled, 10 s
# average. Memory-pressure stalls on memory and keeps I/O flat — the split that
# distinguishes the OOM window from a healthy node.
_PRESSURE_HEALTHY = {
    "cpu_some_avg10": 0.0,
    "io_some_avg10": 0.0,
    "memory_some_avg10": 0.0,
    "memory_full_avg10": 0.0,
}
_PRESSURE_MEMORY = {
    "cpu_some_avg10": 18.6,
    "io_some_avg10": 0.0,
    "memory_some_avg10": 72.4,
    "memory_full_avg10": 41.3,
}

# mem_available_bytes (node-telemetry.md §4.12): the kernel's own "would not
# swap" estimate. Healthy when idle, cratered under memory pressure.
_MEM_AVAILABLE_HEALTHY = 40 * 1024 ** 3
_MEM_AVAILABLE_MEMORY = 512 * 1024 ** 2

# Static §4.13 fields with plausible values. The demo drives no inode or
# conntrack fault, so these hold steady — a real measurement, not a fabrication.
_INODE_USED = 1129359
_INODE_TOTAL = 12976128
_CONNTRACK_COUNT = 457
_CONNTRACK_MAX = 262144


def sample(node_id: str, project_id: str, seq: int, *, ram_used_bytes: int,
           disk_write_latency_ms: float, latency_ms: float, uptime_sec: int,
           vm_ids: list[str], gpu: list | None = None,
           fault: str | None = None,
           oom_kill_count: int = 0, pgmajfault_count: int = 0,
           tcp_retransmit_segs: int = 0, io_ticks_ms: int = 0,
           instance_oom_kill_count: int = 0,
           instance_pgmajfault_count: int = 0) -> dict:
    if fault == FAULT_MEMORY:
        pressure = dict(_PRESSURE_MEMORY)
        mem_available = _MEM_AVAILABLE_MEMORY
        io_in_flight = 0
    else:
        pressure = dict(_PRESSURE_HEALTHY)
        mem_available = _MEM_AVAILABLE_HEALTHY
        io_in_flight = 0
    s = {
        "seq": seq,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "uptime_sec": uptime_sec,
        "cpu_util_pct": 31.2,
        "cpu_core_count": 16,
        "ram_total_bytes": 68719476736,
        "ram_used_bytes": ram_used_bytes,
        "swap_used_bytes": 0,
        "load_avg_1m": 1.42,
        "os_name": "Ubuntu",
        "os_version": "24.04",
        "kernel_version": "6.8.0-142-generic",
        "lxd_cluster_role": "member",
        # hostname / mac_address are C1 — they must arrive as truncated SHA-256
        # hashes (16 hex), not raw values (governance.md §4, redactor gate).
        "hostname": hashlib.sha256(node_id.encode()).hexdigest()[:16],
        "ip_local": "10.14.100.0/24",
        "mac_address": hashlib.sha256(("mac:" + node_id).encode()).hexdigest()[:16],
        "project_id": project_id,
        # §4.12: PSI pressure (where the stall is) + the memory-pressure scalars.
        "pressure": pressure,
        "oom_kill_count": oom_kill_count,
        "pgmajfault_count": pgmajfault_count,
        "mem_available_bytes": mem_available,
        # §4.13: transport / conntrack scalars. Only the count files are C0; the
        # conntrack table is C2 and is never represented here at all.
        "tcp_retransmit_segs": tcp_retransmit_segs,
        "conntrack_count": _CONNTRACK_COUNT,
        "conntrack_max": _CONNTRACK_MAX,
        "disk": [{
            "path": "/dev/sda", "total_bytes": 274877906944, "free_bytes": 214748364800,
            "write_latency_ms": disk_write_latency_ms, "mount_ro": False,
            # §4.13: raw I/O saturation (behind write_latency_ms) + inode table.
            "io_ticks_ms": io_ticks_ms, "io_in_flight": io_in_flight,
            "inode_used": _INODE_USED, "inode_total": _INODE_TOTAL,
        }],
        "net": [{
            "iface": "eth0", "rx_bytes": 123000 + seq, "tx_bytes": 11000 + seq,
            "latency_ms": latency_ms,
            # §4.13: NIC error/drop counters. The demo drives no network fault,
            # so these read 0 — a real "no errors" measurement, not an omission.
            "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0,
        }],
        "services": [
            {"name": "termini-bootstrap", "status": "running"},
            {"name": "lxd", "status": "running"},
            {"name": "sshd", "status": "running"},
        ],
        # Contract field (node-telemetry.md 4.6). The operator's guardrail G5
        # resolves a remediation target against this list (or the literal
        # "heaviest"); with it empty, every model-chosen action is unresolvable.
        # Each vm_id must belong to an instance that EXISTS on the node the
        # executor dials, or the action cannot land (see `advertised_vm_ids`).
        "lxd_instances": [
            {
                "vm_id": vm_id,
                "state": "running",
                "cpu_util_pct": 12.0,
                "ram_used_bytes": max(1, ram_used_bytes // 4),
                "ram_limit_bytes": 4294967296,
                "disk_used_bytes": 2362232012,
                # §4.14: per-instance OOM / major-fault attribution. Only the
                # FIRST advertised instance is the demo's "target" workload, so
                # it is the one whose counters advance under memory pressure;
                # the others are idle (0 is a real measurement here).
                "oom_kill_count": instance_oom_kill_count if i == 0 else 0,
                "pgmajfault_count": instance_pgmajfault_count if i == 0 else 0,
            }
            for i, vm_id in enumerate(vm_ids)
        ],
    }
    if gpu:
        s["gpu"] = gpu
    return s


def envelope(node_id: str, s: dict) -> dict:
    return {
        "_redaction": {"version": "redactor-config v3", "config_hash": "3f2a1b", "result": "ok"},
        "call_id": new_ulid(),
        "node_id": node_id,
        "schema_version": 1,
        "governance_chain_tip": new_ulid(),
        "samples": [s],
    }


class NodeTicker(threading.Thread):
    """Push one telemetry sample per second for a node, oscillating a fault.

    This thread is the ONLY thing keeping a synthetic node ONLINE (the controller
    flips a node to ``heartbeat_late`` after 15 s and OFFLINE after 45 s without a
    fresh ``last_seen_at``), so it may never END while the process lives. A dead
    ticker is indistinguishable from a healthy one from outside the process: `main`
    keeps sleeping, ``systemctl is-active termini-seeder`` keeps reporting ACTIVE,
    and the first symptom is both nodes going OFFLINE roughly 45 s later with no
    error logged anywhere. Every failure path here therefore ends in a retry or a
    log line — never in a `return`.

    ``last_success_monotonic``/``stalled_for()`` cover the *other* half of the same
    defect: a ticker that is still alive but has not landed a sample for a long
    time. That is exactly how the live freeze presented while the controller's
    single SQLite writer was held by the operator's model call.
    """

    def __init__(self, base: str, node: dict, project_id: str,
                 period_s: float, phase: float, high_ram: bool,
                 gpu: list | None = None, *, instance_names: list[str] | None = None,
                 synthetic_ids: bool = False, faults_enabled: bool = True,
                 timeout_s: float = TELEMETRY_TIMEOUT_S,
                 attempts: int = 3, retry_base_s: float = 0.5,
                 max_retry_s: float = 8.0):
        super().__init__(daemon=True)
        self.base = base
        self.node_id = node["node_id"]
        self.token = node["node_token"]
        self.project_id = project_id
        self.period = period_s
        self.phase = phase
        self.high_ram = high_ram
        self.gpu = gpu or []
        # Real LXD instance names on the node the controller will dial, and
        # whether to advertise an invented id instead (see the _INSTANCE_VM_ID
        # block). The advertised set is computed per tick so a restart of the
        # seeder cannot leave the node advertising an id from another node.
        self.instance_names = list(instance_names or [])
        self.synthetic_ids = bool(synthetic_ids)
        self.faults_enabled = bool(faults_enabled)
        self.stop = threading.Event()
        self.timeout_s = timeout_s
        self.attempts = max(1, int(attempts))
        self.retry_base_s = retry_base_s
        self.max_retry_s = max_retry_s
        # The controller DROPS any sample whose seq <= the last seq it stored for
        # that node (out-of-order protection, store.insert_samples). A modulo makes
        # the sequence WRAP: a restart at a different point in the 100000-second
        # cycle emits seq values BELOW what the previous run left behind, so every
        # sample is silently dropped — the dashboard freezes, no threshold fires,
        # no remediation is produced, and nothing is logged as an error. Use the
        # unwrapped epoch so a restart can only ever move the sequence forward.
        self.seq = initial_seq()
        self.sent = 0
        self.failed = 0
        self.consecutive_failures = 0
        self.last_error: str | None = None
        self.last_success_monotonic: float | None = None
        self.started_monotonic = time.monotonic()
        self._logged_error = False
        # §4.12–§4.14 cumulative counters. Monotonic by construction: advanced
        # per tick, never reset, and inherited by a watchdog respawn (like `seq`)
        # so a healed ticker cannot make a counter run backwards mid-demo.
        self.oom_kills = 0
        self.pgmajfaults = 0
        self.tcp_retrans = 0
        self.io_ticks_ms = 0
        self.instance_oom_kills = 0
        self.instance_pgmajfaults = 0
        # Cadence bookkeeping for the OOM killer: fires once every third
        # memory-fault tick, so the demo shows a *pattern* of kills, not a kill
        # on every sample. Counted independently of `seq` so `_advance_counters`
        # is deterministic on its own.
        self._mem_fault_ticks = 0

    # --- sending ----------------------------------------------------------
    def send(self, s: dict) -> bool:
        """POST one sample with bounded retries. True iff the controller took it.

        Never raises for a transport problem: a timeout or an unreachable
        controller says nothing about the SAMPLE, so it is retried with backoff
        (``attempts`` tries), and a 5xx — the controller failing to *store* a valid
        sample, which is what ``database is locked`` behind the operator's model
        call looked like — is retried for the same reason. A 4xx is not retried:
        the sample or the token is wrong and sending it again cannot help.

        When every attempt is spent the tick gives up *for that tick only*; the
        ticker stays alive and tries again. Nothing outside this thread can restart
        it, so giving up permanently is not an option it has.
        """
        delay = self.retry_base_s
        for attempt in range(1, self.attempts + 1):
            try:
                st, body = _request(self.base, "POST",
                                    f"/v1/nodes/{self.node_id}/telemetry",
                                    envelope(self.node_id, s), self.token,
                                    timeout=self.timeout_s)
            except TransportError as e:
                self._failure(str(e), attempt)
                retryable = True
            else:
                if st in TELEMETRY_ACCEPTED:
                    self._success()
                    return True
                self._failure(f"HTTP {st} {body}", attempt)
                retryable = st >= 500
            if not retryable or attempt == self.attempts:
                return False
            self.stop.wait(delay)
            delay = min(delay * 2, self.max_retry_s)
        return False

    def _success(self) -> None:
        self.sent += 1
        if self.consecutive_failures:
            print(f"[seed] telemetry recovered for {self.node_id} after "
                  f"{self.consecutive_failures} failed attempt(s)", flush=True)
        self.consecutive_failures = 0
        self.last_error = None
        self.last_success_monotonic = time.monotonic()
        self._logged_error = False

    def _failure(self, detail: str, attempt: int) -> None:
        self.failed += 1
        self.consecutive_failures += 1
        self.last_error = detail
        # First failure of a streak only — a 1 s cadence against a controller that
        # is busy for minutes would otherwise repeat this line hundreds of times.
        if not self._logged_error:
            print(f"[seed] telemetry FAILED for {self.node_id} "
                  f"(attempt {attempt}/{self.attempts}): {detail}",
                  file=sys.stderr, flush=True)
            self._logged_error = True

    # --- the loop ---------------------------------------------------------
    def tick_delay(self) -> float:
        """Seconds to wait before the next tick: 1 s healthy, backed off on failure.

        The backoff keeps a locked or restarting controller from being hammered by
        a retry storm. It is a cadence, not a surrender: the loop always comes back.
        """
        if self.consecutive_failures == 0:
            return 1.0
        return min(self.retry_base_s * (2 ** min(self.consecutive_failures, 8)),
                   self.max_retry_s)

    def _advance_counters(self, fault: str | None) -> None:
        """Advance the cumulative counters one tick, in line with the fault.

        A quiet node still faults pages and does background I/O, so the healthy
        deltas are small but nonzero. The faulted deltas are an order of magnitude
        larger and touch ONLY the counter that names the stalling resource — that
        is what makes the synthetic sample honest evidence instead of noise.
        """
        self.pgmajfaults += 3
        self.tcp_retrans += 1 if self.seq % 4 == 0 else 0
        self.io_ticks_ms += 40
        if fault == FAULT_MEMORY:
            # Memory-pressure: reclaim thrashes pages to disk (major faults) and
            # the OOM killer fires periodically, hitting the target instance.
            self._mem_fault_ticks += 1
            self.pgmajfaults += 160
            self.instance_pgmajfaults += 130
            if self._mem_fault_ticks % 3 == 0:
                self.oom_kills += 1
                self.instance_oom_kills += 1

    def _tick(self, t0: float) -> None:
        phase = ((time.monotonic() - t0 + self.phase) % self.period) / self.period
        hot = phase < 0.5  # half the period "faulted"
        # Only the memory-pressure (OOM) window remains in the demo fault
        # library. The compute node (high_ram=False) reports healthy at all
        # times; the inference node (high_ram=True) oscillates into the OOM
        # window unless the whole seeder is in quiet mode (--no-faults).
        fault = None
        ram = 18000000000
        disk_lat = 3.0
        net_lat = 0.8
        if self.faults_enabled and self.high_ram and hot:
            fault = FAULT_MEMORY
            ram = 66000000000
        self._advance_counters(fault)
        self.seq += 1
        s = sample(self.node_id, self.project_id, self.seq,
                   ram_used_bytes=ram, disk_write_latency_ms=disk_lat,
                   latency_ms=net_lat, uptime_sec=3600 + int(time.monotonic() - t0),
                   vm_ids=advertised_vm_ids(self.node_id, self.instance_names,
                                            self.synthetic_ids),
                   gpu=self.gpu, fault=fault,
                   oom_kill_count=self.oom_kills,
                   pgmajfault_count=self.pgmajfaults,
                   tcp_retransmit_segs=self.tcp_retrans,
                   io_ticks_ms=self.io_ticks_ms,
                   instance_oom_kill_count=self.instance_oom_kills,
                   instance_pgmajfault_count=self.instance_pgmajfaults)
        self.send(s)

    def run(self) -> None:
        t0 = time.monotonic()
        self.started_monotonic = t0
        while not self.stop.is_set():
            try:
                self._tick(t0)
            except Exception as e:  # noqa: BLE001 — nothing may escape this loop
                # An exception that escapes here ENDS THE THREAD, and that is the
                # defect this guard exists for: the ticker stops, the process keeps
                # running, and the demo freezes with no error systemd can see.
                first_of_streak = not self._logged_error
                self._failure(f"{type(e).__name__}: {e}", 0)
                if first_of_streak:
                    traceback.print_exc()
            self.stop.wait(self.tick_delay())

    # --- observability / self-healing -------------------------------------
    def stalled_for(self) -> float:
        """Seconds since this ticker last landed a sample (or since it started)."""
        ref = self.last_success_monotonic
        if ref is None:
            ref = self.started_monotonic
        return max(0.0, time.monotonic() - ref)

    def respawn(self) -> "NodeTicker":
        """A fresh, NOT-yet-started ticker for the same node.

        ``Thread.start()`` may only be called once, so healing a DEAD ticker means
        replacing the object. The replacement keeps the same node id and token, so
        it never has to register again (bootstrap tokens are single-use) — and it
        keeps the same ``seq``, because restarting the sequence at the epoch value
        can emit a ``seq`` the controller has already stored, which it would drop
        silently (``store.insert_samples`` accepts only an increasing seq).
        """
        fresh = NodeTicker(self.base, {"node_id": self.node_id, "node_token": self.token},
                           self.project_id, self.period, self.phase, self.high_ram,
                           gpu=self.gpu, instance_names=self.instance_names,
                           synthetic_ids=self.synthetic_ids,
                           faults_enabled=self.faults_enabled,
                           timeout_s=self.timeout_s,
                           attempts=self.attempts, retry_base_s=self.retry_base_s,
                           max_retry_s=self.max_retry_s)
        fresh.seq = self.seq
        fresh.sent = self.sent
        fresh.failed = self.failed
        # The cumulative counters must also carry over: a respawned ticker that
        # restarted them at 0 would emit a counter that runs BACKWARDS, which is
        # the same silent corruption a wrapping `seq` causes.
        fresh.oom_kills = self.oom_kills
        fresh.pgmajfaults = self.pgmajfaults
        fresh.tcp_retrans = self.tcp_retrans
        fresh.io_ticks_ms = self.io_ticks_ms
        fresh.instance_oom_kills = self.instance_oom_kills
        fresh.instance_pgmajfaults = self.instance_pgmajfaults
        # If the node has not been updated for minutes, the replacement must inherit
        # that fact — otherwise the freeze timer restarts and the stall goes quiet.
        fresh.last_success_monotonic = self.last_success_monotonic
        return fresh


def watchdog_pass(tickers: list[NodeTicker], *, stall_warn_s: float = DEFAULT_STALL_WARN_S,
                  warned_at: dict | None = None) -> list[NodeTicker]:
    """One supervisor pass over the tickers; returns the list the caller must keep.

    Two shapes of the same freeze are handled, both of which look identical from
    outside the process (unit ACTIVE, no telemetry, nodes OFFLINE ~45 s later):

    * a ticker thread that has DIED is replaced in-process. The replacement reuses
      the node's identity, so no re-registration is involved. The seeder can mint
      fresh bootstrap tokens at startup, so letting systemd restart the whole
      process would *work* — but it would also rotate both node tokens and take the
      nodes OFFLINE for a controller hiccup that this thread can ride out in place;
    * a ticker that is still ALIVE but has not landed a sample for ``stall_warn_s``
      is reported LOUDLY, at most once per window per node (``warned_at`` is the
      caller's mutable dict). The nodes go OFFLINE at 45 s without a sample, so a
      warning at 20 s is the difference between a freeze nobody notices and one
      recorded in the journal.
    """
    if warned_at is None:
        warned_at = {}
    now = time.monotonic()
    out: list[NodeTicker] = []
    for t in tickers:
        if not t.is_alive():
            fresh = t.respawn()
            fresh.start()
            print(f"[seed] WATCHDOG: telemetry thread for {t.node_id} had DIED "
                  f"(last error: {t.last_error or 'none recorded'}); respawned at "
                  f"seq={fresh.seq}", file=sys.stderr, flush=True)
            warned_at.pop(t.node_id, None)
            out.append(fresh)
            continue
        stalled = t.stalled_for()
        if stalled >= stall_warn_s:
            if now - warned_at.get(t.node_id, float("-inf")) >= stall_warn_s:
                print(f"[seed] WATCHDOG: no telemetry accepted for {t.node_id} in "
                      f"{stalled:.0f}s ({t.failed} failed attempt(s), last: "
                      f"{t.last_error}); the node goes OFFLINE at 45s",
                      file=sys.stderr, flush=True)
                warned_at[t.node_id] = now
        else:
            # The node is healthy again: re-arm the warning for the next episode.
            warned_at.pop(t.node_id, None)
        out.append(t)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Seed the W5 demo controller")
    ap.add_argument("--controller", default="http://127.0.0.1:8085")
    ap.add_argument("--token", required=True, help="project_token")
    ap.add_argument("--bootstrap", default="",
                    help="comma-separated single-use bootstrap tokens (optional "
                         "when --bootstrap-mint is given)")
    ap.add_argument("--bootstrap-mint", type=int, default=0, metavar="N",
                    help="mint N fresh single-use bootstrap tokens from the "
                         "controller at startup, so the seeder self-provisions and "
                         "`systemctl start termini-seeder` needs no hand-minted token")
    ap.add_argument("--duration", type=float, default=0.0, help="0 = run until interrupted")
    ap.add_argument("--telemetry-timeout", type=float, default=TELEMETRY_TIMEOUT_S,
                    metavar="S", dest="telemetry_timeout",
                    help="deadline for one telemetry POST; a timeout is RETRIED inside "
                         "the ticker, never fatal")
    ap.add_argument("--stall-warn-seconds", type=float, default=DEFAULT_STALL_WARN_S,
                    metavar="S", dest="stall_warn_seconds",
                    help="warn (once per window) when no telemetry sample has been "
                         "accepted for this long")
    ap.add_argument("--lxd-instance", default=os.environ.get(
                        "TERMINI_LXD_DEFAULT_INSTANCE_NAME", "").strip(),
                    metavar="NAME[,NAME...]", dest="lxd_instance",
                    help="real LXD instance name(s) on the node the controller dials "
                         "(e.g. the GCE node behind TERMINI_LXD_BASE_URL). Their "
                         "controller-issued vm_ids are advertised in lxd_instances[], so "
                         "the model's remediation lands on an instance that EXISTS. "
                         "Defaults to $TERMINI_LXD_DEFAULT_INSTANCE_NAME. Without it the "
                         "seeder advertises an invented id, which the operator's "
                         "fail-closed guard refuses (target_missing) instead of 404ing.")
    ap.add_argument("--synthetic-instance-ids", action="store_true",
                    dest="synthetic_instance_ids",
                    help="advertise an INVENTED instance id even when --lxd-instance is "
                         "given: use only to demonstrate the fail-closed guard")
    ap.add_argument("--no-faults", "--healthy", action="store_true", dest="no_faults",
                    help="quiet mode: the synthetic nodes keep reporting HEALTHY "
                         "telemetry at the normal cadence with no fault window driven, "
                         "so the operator can inject exactly one fault from the console")
    args = ap.parse_args(argv)

    base = args.controller.rstrip("/")
    boots = [t.strip() for t in args.bootstrap.split(",") if t.strip()]
    if args.bootstrap_mint:
        boots.extend(mint_bootstrap_tokens(base, args.token, args.bootstrap_mint))
    if len(boots) < 2:
        raise SystemExit(
            "need at least two bootstrap tokens (compute + inference): pass "
            "--bootstrap a,b or --bootstrap-mint 2"
        )

    # wait for readiness (any HTTP answer means the server is up)
    for _ in range(200):
        try:
            st, _ = _request(base, "GET", "/v1/nodes/register")
            if st is not None:
                break
        except Exception:  # noqa: BLE001 — not up yet
            pass
        time.sleep(0.1)

    node_a = register(base, boots[0], "demo-node-a", [])
    node_c = register(base, boots[1], "demo-node-c",
                      [{"vendor": "nvidia", "model": "NVIDIA L4", "vram_bytes": L4_24GB}])
    project_id = node_a["project_id"]
    instances = [t.strip() for t in args.lxd_instance.split(",") if t.strip()]
    print(f"[seed] project_id = {project_id}", flush=True)
    print(f"[seed] node A (compute)   = {node_a['node_id']}", flush=True)
    print(f"[seed] node C (inference) = {node_c['node_id']}", flush=True)
    # Say out loud which target the demo's remediations will land on: an invented
    # id can only ever be refused by the operator's fail-closed guard, and that is
    # worth knowing before a run rather than during one.
    advertised = advertised_vm_ids(node_a["node_id"], instances,
                                   args.synthetic_instance_ids)
    if instances and not args.synthetic_instance_ids:
        print(f"[seed] lxd_instances[] -> real instances {instances} "
              f"(vm_ids {advertised})", flush=True)
    else:
        print(f"[seed] WARNING: lxd_instances[] advertises an INVENTED vm_id "
              f"{advertised[0]} — no such instance exists on the node the executor "
              f"dials, so the operator will refuse the remediation "
              f"(target_missing). Pass --lxd-instance <real LXD instance> for a "
              f"solvable demo.", file=sys.stderr, flush=True)

    tickers = [
        NodeTicker(base, node_a, project_id, period_s=6.0, phase=0.0, high_ram=False,
                   instance_names=instances,
                   synthetic_ids=args.synthetic_instance_ids,
                   faults_enabled=not args.no_faults,
                   timeout_s=args.telemetry_timeout),
        NodeTicker(base, node_c, project_id, period_s=6.0, phase=3.0, high_ram=True,
                   gpu=GPU_TELEMETRY, instance_names=instances,
                   synthetic_ids=args.synthetic_instance_ids,
                   faults_enabled=not args.no_faults,
                   timeout_s=args.telemetry_timeout),
    ]
    if args.no_faults:
        print("[seed] quiet mode (--no-faults): synthetic nodes report HEALTHY "
              "telemetry only; no fault window is driven.", flush=True)
    for t in tickers:
        t.start()

    # Supervised run loop. Two things can stop telemetry while this process looks
    # perfectly healthy from outside: a ticker thread that died, and a ticker that
    # is alive but getting nothing through. Both produce the same live symptom
    # (unit ACTIVE, dashboard OFFLINE ~45 s later), so both are checked here rather
    # than trusted to the threads. `tickers` is rebound each pass because healing a
    # dead ticker replaces the Thread object (start() is once-only).
    warned_at: dict[str, float] = {}
    try:
        deadline = time.monotonic() + args.duration if args.duration else None
        while deadline is None or time.monotonic() < deadline:
            time.sleep(0.5)
            tickers = watchdog_pass(tickers, stall_warn_s=args.stall_warn_seconds,
                                    warned_at=warned_at)
    except KeyboardInterrupt:
        pass
    finally:
        for t in tickers:
            t.stop.set()
    # Per-node totals: the demo's own evidence that both nodes actually flowed, and
    # the line to read in the journal when the dashboard looks quiet.
    for t in tickers:
        print(f"[seed] {t.node_id}: {t.sent} sample(s) accepted, {t.failed} attempt(s) failed",
              flush=True)
    print("[seed] stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
