#!/usr/bin/env python3
"""Seed a live W5 demo controller with nodes + a 1 s telemetry heartbeat.

Registers a compute node and an inference node with the controller's single-use
bootstrap tokens, then streams telemetry so the dashboard shows live health and
the AI-operator loop has fresh samples to act on. The two synthetic nodes are
driven through an out-of-phase fault pattern (RAM pressure on the inference
node, disk-write latency on the compute node) so the operator emits a steady
stream of log lines — exactly what the dashboard's *live AI-operator log* panel
and the ``/logs`` SSE surface consume.

Runs until interrupted (or ``--duration`` seconds), so start it next to the
controller and the composed console:

    python3 console/seed_demo.py --controller http://127.0.0.1:8085 \\
        --token <project_token> --bootstrap demo-boot-a,demo-boot-c

It is deliberately stdlib-only. Nothing here is required for acceptance — it is
the "make the demo look alive" helper.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

L4_24GB = 24 * 1024 ** 3

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


def _request(base: str, method: str, path: str, body=None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, (json.loads(raw) if raw else None)
        except json.JSONDecodeError:
            return e.code, raw.decode(errors="replace")


def register(base: str, boot_token: str, machine_id: str, gpus: list) -> dict:
    body = {
        "schema_version": "1.0",
        "machine": {
            "machine_id": machine_id,
            "hostname": machine_id,
            "cpu": {"model": "AMD EPYC 7B13", "cores": 8, "threads": 16},
            "memory": {"total_bytes": 68719476736},
            "disks": [{"device": "/dev/sda", "size_bytes": 274877906944}],
            "gpus": gpus,
        },
    }
    st, r = _request(base, "POST", "/v1/nodes/register", body, boot_token)
    if st not in (200, 201) or not isinstance(r, dict):
        raise SystemExit(f"register {machine_id} failed: {st} {r}")
    return r


# Stable synthetic LXD instance id per node.
#
# ``lxd_instances[]`` is a contract field (node-telemetry.md 4.6) and it is the
# ONLY source the operator's guardrail G5 has for resolving a remediation
# target. With the list empty, no model-chosen action can ever resolve, so every
# decision falls back with ``fallback_reason=invalid_target`` — the model is
# consulted and then always discarded. Emitting it makes the demo exercise the
# model-driven path for real.
#
# 26 chars, Crockford-safe (0-9A-F), stable across samples so the model can
# reference the same target in consecutive decisions.
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


def sample(node_id: str, project_id: str, seq: int, *, ram_used_bytes: int,
           disk_write_latency_ms: float, latency_ms: float, uptime_sec: int,
           gpu: list | None = None) -> dict:
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
        "disk": [{"path": "/dev/sda", "total_bytes": 274877906944, "free_bytes": 214748364800,
                  "write_latency_ms": disk_write_latency_ms, "mount_ro": False}],
        "net": [{"iface": "eth0", "rx_bytes": 123000 + seq, "tx_bytes": 11000 + seq,
                 "latency_ms": latency_ms}],
        "services": [
            {"name": "termini-bootstrap", "status": "running"},
            {"name": "lxd", "status": "running"},
            {"name": "sshd", "status": "running"},
        ],
        # Contract field (node-telemetry.md 4.6). The operator's guardrail G5
        # resolves a remediation target against this list (or the literal
        # "heaviest"); with it empty, every model-chosen action is unresolvable.
        "lxd_instances": [
            {
                "vm_id": instance_vm_id(node_id),
                "state": "running",
                "cpu_util_pct": 12.0,
                "ram_used_bytes": max(1, ram_used_bytes // 4),
                "ram_limit_bytes": 4294967296,
                "disk_used_bytes": 2362232012,
            }
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
    """Push one telemetry sample per second for a node, oscillating a fault."""

    def __init__(self, base: str, node: dict, project_id: str,
                 period_s: float, phase: float, high_ram: bool,
                 gpu: list | None = None):
        super().__init__(daemon=True)
        self.base = base
        self.node_id = node["node_id"]
        self.token = node["node_token"]
        self.project_id = project_id
        self.period = period_s
        self.phase = phase
        self.high_ram = high_ram
        self.gpu = gpu or []
        self.stop = threading.Event()
        # The controller DROPS any sample whose seq <= the last seq it stored for
        # that node (out-of-order protection, store.insert_samples). A modulo makes
        # the sequence WRAP: a restart at a different point in the 100000-second
        # cycle emits seq values BELOW what the previous run left behind, so every
        # sample is silently dropped — the dashboard freezes, no threshold fires,
        # no remediation is produced, and nothing is logged as an error. Use the
        # unwrapped epoch so a restart can only ever move the sequence forward.
        self.seq = initial_seq()
        self.sent = 0
        self._logged_error = False

    def run(self) -> None:
        t0 = time.monotonic()
        while not self.stop.is_set():
            phase = ((time.monotonic() - t0 + self.phase) % self.period) / self.period
            hot = phase < 0.5  # half the period "faulted"
            ram = 66000000000 if (self.high_ram and hot) else 18000000000
            disk_lat = 120.0 if (not self.high_ram and hot) else 3.0
            net_lat = 0.8
            self.seq += 1
            s = sample(self.node_id, self.project_id, self.seq,
                       ram_used_bytes=ram, disk_write_latency_ms=disk_lat,
                       latency_ms=net_lat, uptime_sec=3600 + int(time.monotonic() - t0),
                       gpu=self.gpu)
            st, _ = _request(self.base, "POST", f"/v1/nodes/{self.node_id}/telemetry",
                             envelope(self.node_id, s), self.token)
            if st in (200, 202):
                self.sent += 1
            elif not self._logged_error:
                print(f"[seed] telemetry rejected for {self.node_id}: {st} {_}",
                      file=sys.stderr, flush=True)
                self._logged_error = True
            self.stop.wait(1.0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Seed the W5 demo controller")
    ap.add_argument("--controller", default="http://127.0.0.1:8085")
    ap.add_argument("--token", required=True, help="project_token")
    ap.add_argument("--bootstrap", required=True, help="comma-separated single-use bootstrap tokens")
    ap.add_argument("--duration", type=float, default=0.0, help="0 = run until interrupted")
    args = ap.parse_args(argv)

    base = args.controller.rstrip("/")
    boots = [t.strip() for t in args.bootstrap.split(",") if t.strip()]
    if len(boots) < 2:
        raise SystemExit("need at least two bootstrap tokens (compute + inference)")

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
    print(f"[seed] project_id = {project_id}", flush=True)
    print(f"[seed] node A (compute)   = {node_a['node_id']}", flush=True)
    print(f"[seed] node C (inference) = {node_c['node_id']}", flush=True)

    tickers = [
        NodeTicker(base, node_a, project_id, period_s=6.0, phase=0.0, high_ram=False),
        NodeTicker(base, node_c, project_id, period_s=6.0, phase=3.0, high_ram=True,
                   gpu=GPU_TELEMETRY),
    ]
    for t in tickers:
        t.start()

    try:
        deadline = time.monotonic() + args.duration if args.duration else None
        while deadline is None or time.monotonic() < deadline:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for t in tickers:
            t.stop.set()
    print("[seed] stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
