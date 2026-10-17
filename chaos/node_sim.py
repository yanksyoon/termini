#!/usr/bin/env python3
"""Simulated Termini node — registers with the controller and posts telemetry.

This is a **test simulator, not the shipped node agent**. It stands in for the
on-node `termini-bootstrap` daemon described in
`docs/contracts/node-bootstrap.md`, which is *specified but not implemented* in
this repo: there is no `termini-bootstrap` binary, systemd unit, or on-node
redactor in the tree, and not one field it emits below is real.

What that means for anything it emits:

* The `_redaction` envelope and the `services[]` entry named
  `termini-bootstrap` are **fabricated** here. They are required by the frozen
  telemetry contract (`docs/telemetry-schema.md`, `controller/app/fields.py`),
  which *names* the planned daemon/redactor — the simulator does not run one.
* Content-based (C2) governance is enforced **controller-side**, not on the
  node: a non-conforming batch is rejected whole with `422 GOVERNANCE_REJECT`
  (`controller/app/governance.py scan_c2` -> `controller/app/server.py
  _ingest_telemetry`). There is no node-side "drop the field before the socket
  opens" step in this build.

It posts over the frozen wire contract (docs/contracts/node-telemetry.md,
docs/telemetry-schema.md) so the control plane can be driven through a
*reproducible* failure series instead of waiting for a real fault.

Modes (`--mode`), all cadence- and threshold-driven from chaos/chaos.json:

    healthy       steady baseline (false-positive guard) — nothing crosses a rule
    oom           ramp ram_used_bytes past the OOM threshold, then hold it
    disk-fail     flip a disk mount read-only (EXT4 remount-ro, Scenario B)

Register-only usage (no telemetry loop) is `--samples 0 --duration 0`.

Standalone:
    python3 chaos/node_sim.py --url http://127.0.0.1:18090 \
        --bootstrap-token "$BOOT" --mode oom --duration 8

Or through the harness: `make chaos SCENARIO=oom`.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # allow `python3 chaos/node_sim.py`

from chaos import common  # noqa: E402

L4_24GB = 24 * 1024 ** 3
GiB = 1024 ** 3


# --------------------------------------------------------------------------- #
# Registration (canonical shape, node-bootstrap.md §5.2)
# --------------------------------------------------------------------------- #
def build_registration(hostname: str, gpu: bool, machine_id: str | None = None) -> dict:
    gpus = (
        [{"vendor": "nvidia", "model": "NVIDIA L4", "vram_bytes": L4_24GB, "health": "ok"}]
        if gpu
        else []
    )
    return {
        "schema_version": "1.0",
        "machine": {
            "machine_id": machine_id or f"{hostname}-machine",
            "hostname": hostname,
            "image_version": "2026.10.01-chaos",
            "os": {"name": "ubuntu", "version": "24.04.1"},
            "lxd": {"version": "5.21.3", "snap_revision": "31234"},
            "cpu": {"model": "AMD EPYC 7B13", "sockets": 1, "cores": 8, "threads": 16},
            "memory": {"total_bytes": 68719476736},
            "disks": [{"device": "/dev/sda", "type": "ssd", "size_bytes": 274877906944, "health": "ok"}],
            "gpus": gpus,
            "role_hint": "auto",
        },
        "host_ssh_keys": [f"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI root@{hostname}"],
        "agent": {"version": "0.1.0-chaos", "boot_id": "8f0e"},
    }


# --------------------------------------------------------------------------- #
# Sample generation
# --------------------------------------------------------------------------- #
def generate_sample(
    *,
    node: dict,
    project_id: str,
    vm_id: str,
    mode: str,
    params: dict,
    seq: int,
    elapsed_s: float,
    ts: str,
) -> dict:
    """One Sample object for the given mode/time. Pure — unit-testable."""
    ram_total = int(node["ram_total_bytes"])
    disk_total = int(node["disk_total_bytes"])
    gpu = bool(node.get("gpu"))

    # --- healthy baseline -------------------------------------------------- #
    ram_pct = 62.0
    cpu_pct = 31.2
    free_pct = 78.0
    write_latency_ms = 3.0
    net_latency_ms = 0.8
    instance_ram_pct = 55.0
    instance_disk_pct = 20.0
    mount_ro = False

    if mode == "oom":
        ramp = max(0.1, float(params.get("ramp_s", 3.0)))
        frac = min(1.0, elapsed_s / ramp)
        start = float(params.get("ram_start_pct", 62.0))
        peak = float(params.get("ram_peak_pct", 97.0))
        ram_pct = start + (peak - start) * frac
        cpu_pct = 40.0 + 55.0 * frac
        instance_ram_pct = 60.0 + 38.0 * frac
    elif mode == "disk-fail":
        # Scenario B: EXT4 remount read-only. The mount flips read-only shortly
        # after injection and stays ro for the rest of the run.
        fail_after = float(params.get("fail_after_s", 1.0))
        mount_ro = elapsed_s >= fail_after
    elif mode != "healthy":
        raise ValueError(f"unknown mode {mode!r}")

    ram_used = int(ram_total * max(0.0, min(ram_pct, 99.9)) / 100.0)
    free_bytes = int(disk_total * max(0.0, min(free_pct, 100.0)) / 100.0)

    sample = {
        "seq": int(seq),
        "ts": ts,
        "uptime_sec": int(3600 + seq),
        "cpu_util_pct": round(cpu_pct, 2),
        "cpu_core_count": int(node["cpu_core_count"]),
        "ram_total_bytes": ram_total,
        "ram_used_bytes": ram_used,
        "swap_used_bytes": 0,
        "load_avg_1m": round(1.42 + seq * 0.01, 3),
        "cpu_temp_celsius": round(51.0 + (cpu_pct - 31.2) * 0.2, 2),
        "os_name": "Ubuntu",
        "os_version": "24.04",
        "kernel_version": "6.8.0-142-generic",
        "lxd_cluster_role": "member",
        "hostname": "5f2a9c1e7b4d4a1f",
        "ip_local": "10.0.0.0/24",
        "mac_address": "9c1e7b4d4a1f5f2a",
        "project_id": project_id,
        "disk": [
            {
                "path": "/dev/sda",
                "total_bytes": disk_total,
                "free_bytes": free_bytes,
                "write_latency_ms": round(write_latency_ms, 2),
                "mount_ro": mount_ro,
            }
        ],
        "net": [{"iface": "eth0", "rx_bytes": 123000 + seq * 100, "tx_bytes": 11000, "latency_ms": round(net_latency_ms, 2)}],
        "lxd_instances": [
            {
                "vm_id": vm_id,
                "state": "running",
                "cpu_util_pct": round(min(cpu_pct, 99.0), 2),
                "ram_used_bytes": int(4 * GiB * instance_ram_pct / 100.0),
                "ram_limit_bytes": 4 * GiB,
                "disk_used_bytes": int(8 * GiB * instance_disk_pct / 100.0),
            }
        ],
        "services": [
            # `termini-bootstrap` is the enum value the frozen telemetry contract
            # reserves for the (planned, unshipped) on-node daemon
            # (controller/app/fields.py SERVICE_FIELDS). The simulator reports it
            # as running to satisfy that contract; no such service exists here.
            {"name": "termini-bootstrap", "status": "running"},
            {"name": "lxd", "status": "running"},
            {"name": "sshd", "status": "running"},
        ],
    }
    if gpu:
        sample["gpu"] = [
            {
                "model": "NVIDIA L4",
                "util_pct": 88.0,
                "vram_total_bytes": L4_24GB,
                "vram_used_bytes": 12884901888,
                "temp_celsius": 62.0,
            }
        ]
        sample["services"].append({"name": "llama-cpp", "status": "running"})
        sample["inference"] = {
            "server_up": True,
            "model": "deepseek-flash",
            "active_requests": 3,
            "queue_depth": 0,
            "tokens_per_s": 48.0,
            "vram_used_bytes": 12884901888,
        }
    return sample


def build_batch(node_id: str, sample: dict) -> dict:
    # `_redaction` is synthetic: the frozen envelope requires the field
    # (controller/app/fields.py ENVELOPE_REQUIRED), so the simulator asserts a
    # redactor result it cannot actually produce. No on-node redactor is shipped;
    # real C2 enforcement is the controller-side scan_c2 gate (422).
    return {
        "_redaction": {"version": "redactor-config v3", "config_hash": "3f2a1b", "result": "ok"},
        "call_id": common.new_ulid(),
        "node_id": node_id,
        "schema_version": 1,
        "governance_chain_tip": common.new_ulid(),
        "samples": [sample],
    }


def register(base: str, token: str, hostname: str, gpu: bool, machine_id: str | None = None) -> tuple[int, dict]:
    st, body = common.http_json(
        "POST", f"{base}/v1/nodes/register", build_registration(hostname, gpu, machine_id), token=token
    )
    return st, body if isinstance(body, dict) else {}


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(
    *,
    base: str,
    bootstrap_token: str,
    node: dict,
    mode: str,
    params: dict,
    duration_s: float,
    cadence_ms: int,
    series_out: str | None = None,
    summary_out: str | None = None,
    log=print,
) -> dict:
    st, reg = register(base, bootstrap_token, node["hostname"], bool(node.get("gpu")))
    if st not in (200, 201) or "node_id" not in reg:
        raise RuntimeError(f"registration failed: HTTP {st} {reg}")
    node_id = reg["node_id"]
    node_token = reg["node_token"]
    project_id = reg["project_id"]
    vm_id = common.new_ulid()

    summary = {
        "mode": mode,
        "hostname": node["hostname"],
        "node_id": node_id,
        "node_token": node_token,  # harness scratch only; never committed
        "project_id": project_id,
        "role": reg.get("role"),
        "vm_id": vm_id,
        "cadence_ms": cadence_ms,
        "duration_s": duration_s,
        "started_at": common.rfc3339_now(),
        "samples_posted": 0,
        "samples_rejected": 0,
    }
    if summary_out:
        common.write_json(summary_out, summary)

    log(f"[node_sim] registered {node['hostname']} node_id={node_id} role={reg.get('role')} mode={mode}")

    series: list[dict] = []
    seq = 1
    started = time.monotonic()
    next_at = 0.0
    try:
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= duration_s:
                break
            ts = common.rfc3339_now()
            sample = generate_sample(
                node=node, project_id=project_id, vm_id=vm_id, mode=mode,
                params=params, seq=seq, elapsed_s=elapsed, ts=ts,
            )
            batch = build_batch(node_id, sample)
            st, resp = common.http_json("POST", f"{base}/v1/nodes/{node_id}/telemetry", batch, token=node_token)
            if st == 202:
                series.append(sample)
            else:
                summary["samples_rejected"] += 1
                log(f"[node_sim] seq={seq} telemetry -> HTTP {st} {resp}")
            seq += 1
            # sleep until the next cadence tick (never busy-spin at the tail)
            next_at += cadence_ms / 1000.0
            sleep_for = next_at - (time.monotonic() - started)
            if sleep_for > 0:
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        pass

    summary["samples_posted"] = len(series)
    summary["finished_at"] = common.rfc3339_now()
    if series:
        summary["first_ts"] = series[0]["ts"]
        summary["last_ts"] = series[-1]["ts"]
    if series_out:
        common.write_json(series_out, series)
    if summary_out:
        common.write_json(summary_out, summary)
    log(f"[node_sim] {mode}: posted {len(series)} samples (rejected {summary['samples_rejected']})")
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Simulated Termini node (chaos fault injection)")
    ap.add_argument("--url", required=True, help="controller base URL")
    ap.add_argument("--bootstrap-token", required=True)
    ap.add_argument("--hostname", default="node-chaos")
    ap.add_argument("--mode", default="healthy", choices=["healthy", "oom", "disk-fail"])
    ap.add_argument("--duration", type=float, default=8.0, help="seconds to stream")
    ap.add_argument("--cadence-ms", type=int, default=1000)
    ap.add_argument("--gpu", action="store_true", help="advertise a GPU (inference role)")
    ap.add_argument("--params", default="{}", help="JSON string of mode parameters")
    ap.add_argument("--ram-total-bytes", type=int, default=34359738368)
    ap.add_argument("--disk-total-bytes", type=int, default=274877906944)
    ap.add_argument("--series-out", default=None, help="write emitted samples (JSON) here")
    ap.add_argument("--summary-out", default=None, help="write node/run summary (JSON) here")
    args = ap.parse_args(argv)

    node = {
        "hostname": args.hostname,
        "cpu_core_count": 16,
        "ram_total_bytes": args.ram_total_bytes,
        "disk_total_bytes": args.disk_total_bytes,
        "gpu": args.gpu,
    }
    try:
        params = json.loads(args.params)
    except json.JSONDecodeError as exc:
        print(f"--params is not valid JSON: {exc}", file=sys.stderr)
        return 2

    try:
        run(
            base=args.url.rstrip("/"),
            bootstrap_token=args.bootstrap_token,
            node=node,
            mode=args.mode,
            params=params,
            duration_s=args.duration,
            cadence_ms=args.cadence_ms,
            series_out=args.series_out,
            summary_out=args.summary_out,
        )
    except RuntimeError as exc:
        print(f"[node_sim] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
