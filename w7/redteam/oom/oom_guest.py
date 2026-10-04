#!/usr/bin/env python3
"""OOM red-team guest simulator — streams telemetry for a memory ramp **and models
the kernel OOM-killer firing**.

This exists to reproduce the race flagged in `termini/project.md` §4:

    stress-ng allocates memory at the Linux kernel level inside the target guest
    VM. Unless LXD/cgroups limits are strict, the Linux Kernel Out-Of-Memory
    (OOM) Killer will simply terminate the `stress-ng` process inside the VM
    before the GCP Controller even receives its telemetry polling cycle.

The simulator makes the interleaving explicit and observable:

  * it ramps `ram_used_bytes` from `ram_start_pct` to `ram_peak_pct` over
    `ramp_s` seconds and (optionally) holds the peak;
  * when the modelled utilisation reaches `kill_pct` (default 100 %, i.e. the
    kernel has nothing left to reclaim) the kernel OOM-kills the stressing
    process: memory is immediately released and every subsequent sample reports
    the healthy baseline. A `kill` marker (with its timestamp and the sequence
    number of the last breaching sample) is written so a failure can be
    attributed to the *kill timing*, not merely "it crashed";
  * every emitted sample is appended to `--series-out` with its wall-clock
    timestamp so detection latency can be recomputed offline.

If `kill_pct` is set above `ram_peak_pct` (or `--no-kill` is passed) the guest
never dies — this is the *capped* ramp the shipped chaos scenario uses
(`ram_peak_pct: 97`), which deliberately leaves an open remediation window.

It registers through the frozen node-bootstrap/telemetry wire contract by
reusing `chaos.node_sim`, so the controller treats it exactly like a real node.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

_REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "chaos"))

from chaos import common  # noqa: E402
from chaos import node_sim  # noqa: E402

RAM_TOTAL = 34_359_738_368


def _ram_pct_at(elapsed_s: float, *, start: float, peak: float, ramp_s: float) -> float:
    frac = min(1.0, max(0.0, elapsed_s / max(1e-6, ramp_s)))
    return start + (peak - start) * frac


def run(
    *,
    base: str,
    bootstrap_token: str,
    hostname: str,
    start_pct: float,
    peak_pct: float,
    ramp_s: float,
    hold_s: float,
    kill_pct: float | None,
    post_kill_pct: float,
    cadence_ms: int,
    series_out: str,
    kill_out: str,
    summary_out: str,
    log=print,
) -> dict:
    st, reg = node_sim.register(base, bootstrap_token, hostname, gpu=False)
    if st not in (200, 201) or "node_id" not in reg:
        raise RuntimeError(f"registration failed: HTTP {st} {reg}")
    node_id = reg["node_id"]
    node_token = reg["node_token"]
    project_id = reg["project_id"]
    vm_id = common.new_ulid()

    node = {
        "hostname": hostname,
        "cpu_core_count": 16,
        "ram_total_bytes": RAM_TOTAL,
        "disk_total_bytes": 274_877_906_944,
        "gpu": False,
    }

    summary = {
        "mode": "oom-race",
        "hostname": hostname,
        "node_id": node_id,
        "project_id": project_id,
        "role": reg.get("role"),
        "vm_id": vm_id,
        "ram_start_pct": start_pct,
        "ram_peak_pct": peak_pct,
        "ramp_s": ramp_s,
        "hold_s": hold_s,
        "kill_pct": kill_pct,
        "post_kill_pct": post_kill_pct,
        "cadence_ms": cadence_ms,
        "samples_posted": 0,
        "killed": False,
    }
    common.write_json(summary_out, summary)
    log(f"[guest] registered node_id={node_id} role={reg.get('role')} "
        f"ramp {start_pct}->{peak_pct}% over {ramp_s}s kill_pct={kill_pct}")

    series: list[dict] = []
    wall: list[dict] = []          # ts + seq + ram_pct + knee (pre/post kill)
    killed = False
    kill_ts = None
    kill_seq = None
    last_breach_seq = None
    seq = 1
    started = time.monotonic()
    total_s = ramp_s + hold_s
    next_at = 0.0

    try:
        while True:
            elapsed = time.monotonic() - started
            if elapsed > total_s + 1.0:
                break
            ts = common.rfc3339_now()

            if killed:
                sample = node_sim.generate_sample(
                    node=node, project_id=project_id, vm_id=vm_id,
                    mode="healthy", params={}, seq=seq, elapsed_s=elapsed, ts=ts,
                )
                # Force the post-kill healthy baseline.
                sample["ram_used_bytes"] = int(RAM_TOTAL * post_kill_pct / 100.0)
                knee = "post_kill"
            else:
                ram_pct = _ram_pct_at(elapsed, start=start_pct, peak=peak_pct, ramp_s=ramp_s)
                if kill_pct is not None and ram_pct >= kill_pct:
                    # The kernel fires *between* samples: this sample is already
                    # the post-kill one, and the peak was the previous sample.
                    killed = True
                    kill_ts = ts
                    kill_seq = last_breach_seq
                    summary["killed"] = True
                    summary["kill_ts"] = kill_ts
                    common.write_json(summary_out, summary)
                    log(f"[guest] KERNEL OOM-KILL modelled at t={elapsed:.2f}s "
                        f"(last breaching sample seq={kill_seq}); ram released -> "
                        f"{post_kill_pct}%")
                    sample = node_sim.generate_sample(
                        node=node, project_id=project_id, vm_id=vm_id,
                        mode="healthy", params={}, seq=seq, elapsed_s=elapsed, ts=ts,
                    )
                    sample["ram_used_bytes"] = int(RAM_TOTAL * post_kill_pct / 100.0)
                    knee = "post_kill"
                else:
                    sample = node_sim.generate_sample(
                        node=node, project_id=project_id, vm_id=vm_id,
                        mode="oom",
                        params={"ram_start_pct": start_pct, "ram_peak_pct": peak_pct,
                                "ramp_s": ramp_s},
                        seq=seq, elapsed_s=elapsed, ts=ts,
                    )
                    # Recompute with the actual ramp parameters (generate_sample
                    # already does this, keep it explicit for the record).
                    sample["ram_used_bytes"] = int(RAM_TOTAL * ram_pct / 100.0)
                    knee = "breach" if ram_pct >= 95.0 else "normal"
                    if ram_pct >= 95.0:
                        last_breach_seq = seq

            batch = node_sim.build_batch(node_id, sample)
            st, resp = common.http_json(
                "POST", f"{base}/v1/nodes/{node_id}/telemetry", batch, token=node_token
            )
            if st == 202:
                series.append(sample)
                wall.append({"seq": seq, "ts": ts, "elapsed_s": round(elapsed, 3),
                             "ram_pct": round(sample["ram_used_bytes"] / RAM_TOTAL * 100, 2),
                             "knee": knee})
            else:
                log(f"[guest] telemetry seq={seq} -> HTTP {st} {resp}")
            seq += 1
            next_at += cadence_ms / 1000.0
            sleep_for = next_at - (time.monotonic() - started)
            if sleep_for > 0:
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        pass

    common.write_json(series_out, series)
    common.write_json(kill_out, {
        "killed": killed,
        "kill_ts": kill_ts,
        "kill_seq": kill_seq,
        "last_breach_seq": last_breach_seq,
        "wall": wall,
    })
    summary["samples_posted"] = len(series)
    summary["finished_at"] = common.rfc3339_now()
    common.write_json(summary_out, summary)
    log(f"[guest] posted {len(series)} samples, killed={killed}")
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="OOM red-team guest simulator (models the kernel OOM kill)")
    ap.add_argument("--url", required=True)
    ap.add_argument("--bootstrap-token", required=True)
    ap.add_argument("--hostname", default="node-oom-race")
    ap.add_argument("--ram-start-pct", type=float, default=62.0)
    ap.add_argument("--ram-peak-pct", type=float, default=100.0)
    ap.add_argument("--ramp-s", type=float, default=8.0)
    ap.add_argument("--hold-s", type=float, default=6.0)
    ap.add_argument("--kill-pct", type=float, default=100.0,
                    help="utilisation at which the kernel OOM-kills (set > peak to disable)")
    ap.add_argument("--no-kill", action="store_true")
    ap.add_argument("--post-kill-pct", type=float, default=62.0)
    ap.add_argument("--cadence-ms", type=int, default=1000)
    ap.add_argument("--series-out", required=True)
    ap.add_argument("--kill-out", required=True)
    ap.add_argument("--summary-out", required=True)
    args = ap.parse_args(argv)

    kill_pct = None if args.no_kill else args.kill_pct
    try:
        run(
            base=args.url.rstrip("/"),
            bootstrap_token=args.bootstrap_token,
            hostname=args.hostname,
            start_pct=args.ram_start_pct,
            peak_pct=args.ram_peak_pct,
            ramp_s=args.ramp_s,
            hold_s=args.hold_s,
            kill_pct=kill_pct,
            post_kill_pct=args.post_kill_pct,
            cadence_ms=args.cadence_ms,
            series_out=args.series_out,
            kill_out=args.kill_out,
            summary_out=args.summary_out,
        )
    except RuntimeError as exc:
        print(f"[guest] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
