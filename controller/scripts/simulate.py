#!/usr/bin/env python3
"""Simulated node — registers with the controller and streams telemetry.

Exercises the exact wire contract a real bootstrap daemon sends:

    python3 scripts/simulate.py \
        --url http://127.0.0.1:8080 \
        --bootstrap-token "$BOOT" \
        --hostname node-a \
        [--gpu] [--interval 1.0] [--samples 0]

`samples 0` means stream forever; otherwise stop after N samples.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

CONTROLLER_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CONTROLLER_DIR))

from app import ulid  # noqa: E402

L4_24GB = 24 * 1024 ** 3


def post(url: str, body: dict, token: str | None = None) -> tuple[int, object]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw.decode()


def registration(hostname: str, gpu: bool) -> dict:
    gpus = [{"vendor": "nvidia", "model": "NVIDIA L4", "vram_bytes": L4_24GB, "health": "ok"}] if gpu else []
    return {
        "schema_version": "1.0",
        "machine": {
            "machine_id": f"{hostname}-machine",
            "hostname": hostname,
            "image_version": "2026.10.01-abc1234",
            "os": {"name": "ubuntu", "version": "24.04.1"},
            "lxd": {"version": "5.21.3", "snap_revision": "31234"},
            "cpu": {"model": "AMD EPYC 7B13", "sockets": 1, "cores": 8, "threads": 16},
            "memory": {"total_bytes": 68719476736},
            "disks": [{"device": "/dev/sda", "type": "ssd", "size_bytes": 214748364800, "health": "ok"}],
            "gpus": gpus,
            "role_hint": "auto",
        },
        "host_ssh_keys": [f"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI root@{hostname}"],
        "agent": {"version": "0.1.0", "boot_id": "8f0e"},
    }


def batch(node_id: str, seq: int, gpu: bool) -> dict:
    sample = {
        "seq": seq,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "uptime_sec": seq,
        "cpu_util_pct": 31.2 + (seq % 10) / 10,
        "cpu_core_count": 16,
        "ram_total_bytes": 34359738368,
        "ram_used_bytes": 19756849561 + seq * 1000,
        "swap_used_bytes": 0,
        "load_avg_1m": 1.42,
        "os_name": "Ubuntu",
        "os_version": "24.04",
        "kernel_version": "6.8.0-142-generic",
        "lxd_cluster_role": "member",
        "hostname": "5f2a9c1e7b4d4a1f",
        "ip_local": "10.0.0.0/24",
        "mac_address": "9c1e7b4d4a1f5f2a",
        "project_id": ulid.new_ulid(),
        "disk": [{"path": "/dev/sda", "total_bytes": 274877906944, "free_bytes": 214748364800,
                  "write_latency_ms": 3.0, "mount_ro": False}],
        "net": [{"iface": "eth0", "rx_bytes": 123000, "tx_bytes": 11000, "latency_ms": 0.8}],
    }
    if gpu:
        sample["gpu"] = [{"model": "NVIDIA L4", "util_pct": 88.0, "vram_total_bytes": L4_24GB,
                          "vram_used_bytes": 12884901888, "temp_celsius": 62.0}]
    return {
        "_redaction": {"version": "redactor-config v3", "config_hash": "3f2a1b", "result": "ok"},
        "call_id": ulid.new_ulid(),
        "node_id": node_id,
        "schema_version": 1,
        "governance_chain_tip": ulid.new_ulid(),
        "samples": [sample],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--bootstrap-token", required=True)
    ap.add_argument("--hostname", default="node-a")
    ap.add_argument("--gpu", action="store_true")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--samples", type=int, default=0, help="0 = stream forever")
    args = ap.parse_args()

    base = args.url.rstrip("/")
    st, r = post(f"{base}/v1/nodes/register", registration(args.hostname, args.gpu), token=args.bootstrap_token)
    if st != 201:
        print(f"registration failed: {st} {r}")
        return 1
    node_id, node_token, role = r["node_id"], r["node_token"], r["role"]
    print(f"registered {args.hostname} -> node_id={node_id} role={role}")

    seq = 0
    try:
        while True:
            st, resp = post(f"{base}/v1/nodes/{node_id}/telemetry", batch(node_id, seq, args.gpu), token=node_token)
            print(f"  seq={seq} -> {st} {resp if st != 202 else ''}")
            seq += 1
            if args.samples and seq >= args.samples:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
