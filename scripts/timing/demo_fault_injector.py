#!/usr/bin/env python3
"""demo_fault_injector.py — inject a deterministic node fault for the demo dry run.

This reuses the chaos harness's sample generator (`chaos/node_sim.py`) so the
emitted telemetry is byte-for-byte the frozen schema the controller validates,
but it OMITS the synthetic `lxd_instances` inventory that the simulator adds.

Why: the AI operator resolves a playbook's `target: heaviest` workload from the
`lxd_instances` telemetry field. The chaos simulator invents that inventory
(and a random `vm_id`), so on its runs the remediation targets a synthetic
workload id. The live demo nodes stream *real* node telemetry with no nested
LXD inventory — there the operator falls back to its configured default
instance name, which the demo wires to the node itself. This injector reproduces
that live shape, so the dry run's remediation lands on a *real* LXD instance.

It is a thin wrapper: registration, batching, cadence and the sample series all
come from `chaos.node_sim` / `chaos.common`.

Usage:
  demo_fault_injector.py --url http://127.0.0.1:18085 \\
      --bootstrap-token demo-boot-a --mode oom --duration 12
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from chaos import common, node_sim  # noqa: E402


def inject(
    *,
    base: str,
    bootstrap_token: str,
    hostname: str,
    mode: str,
    params: dict,
    duration_s: float,
    cadence_ms: int,
    series_out: str | None = None,
    summary_out: str | None = None,
    log=print,
) -> dict:
    node = {
        "hostname": hostname,
        "cpu_core_count": 8,
        "ram_total_bytes": 34359738368,
        "disk_total_bytes": 274877906944,
        "gpu": False,
    }
    st, reg = node_sim.register(base, bootstrap_token, hostname, False)
    if st not in (200, 201) or "node_id" not in reg:
        raise RuntimeError(f"registration failed: HTTP {st} {reg}")
    node_id = reg["node_id"]
    node_token = reg["node_token"]
    project_id = reg["project_id"]

    summary = {
        "mode": mode,
        "hostname": hostname,
        "node_id": node_id,
        "project_id": project_id,
        "role": reg.get("role"),
        "cadence_ms": cadence_ms,
        "duration_s": duration_s,
        "started_at": common.rfc3339_now(),
        "samples_posted": 0,
        "samples_rejected": 0,
    }
    if summary_out:
        common.write_json(summary_out, summary)
    log(f"[inject] registered {hostname} node_id={node_id} role={reg.get('role')} mode={mode}")

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
            sample = node_sim.generate_sample(
                node=node, project_id=project_id, vm_id="", mode=mode,
                params=params, seq=seq, elapsed_s=elapsed, ts=ts,
            )
            # The only divergence from the simulator: no synthetic workload
            # inventory, so `target: heaviest` falls back to the node itself.
            sample.pop("lxd_instances", None)
            batch = node_sim.build_batch(node_id, sample)
            st, resp = common.http_json(
                "POST", f"{base}/v1/nodes/{node_id}/telemetry", batch, token=node_token
            )
            if st == 202:
                series.append(sample)
            else:
                summary["samples_rejected"] += 1
                log(f"[inject] seq={seq} telemetry -> HTTP {st} {resp}")
            seq += 1
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
    log(f"[inject] {mode}: posted {len(series)} samples (rejected {summary['samples_rejected']})")
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Demo fault injector (no workload inventory)")
    ap.add_argument("--url", required=True)
    ap.add_argument("--bootstrap-token", required=True)
    ap.add_argument("--hostname", default="demo-fault-node")
    ap.add_argument("--mode", required=True, choices=["healthy", "oom", "disk-fail"])
    ap.add_argument("--duration", type=float, default=12.0)
    ap.add_argument("--cadence-ms", type=int, default=1000)
    ap.add_argument("--params", default="{}")
    ap.add_argument("--series-out", default=None)
    ap.add_argument("--summary-out", default=None)
    args = ap.parse_args(argv)
    try:
        params = json.loads(args.params)
    except json.JSONDecodeError as exc:
        print(f"--params is not valid JSON: {exc}", file=sys.stderr)
        return 2
    try:
        inject(
            base=args.url.rstrip("/"),
            bootstrap_token=args.bootstrap_token,
            hostname=args.hostname,
            mode=args.mode,
            params=params,
            duration_s=args.duration,
            cadence_ms=args.cadence_ms,
            series_out=args.series_out,
            summary_out=args.summary_out,
        )
    except RuntimeError as exc:
        print(f"[inject] ERROR {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
