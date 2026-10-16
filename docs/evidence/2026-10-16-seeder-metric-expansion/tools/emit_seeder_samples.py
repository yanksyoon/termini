#!/usr/bin/env python3
"""Emit the demo seeder's raw §4.12–§4.14 samples and self-check them (t_a1b5e136).

The seeder (``console/seed_demo.py``) synthesises its own telemetry; this tool
exercises the SAME functions the ticker calls so the committed evidence is
byte-for-byte what the demo would put on the wire — not a hand-edited copy.

It loads ``console/seed_demo.py`` itself (no copy, no stub), builds one raw sample
per fault window (healthy / disk-degrade / memory-pressure), drives the ticker's
``_advance_counters`` a few ticks to show the cumulative counters advancing only
under the fault that names them, then scans every sample the way the contract
says egress is scanned.

    python3 docs/evidence/2026-10-16-seeder-metric-expansion/tools/emit_seeder_samples.py

Outputs (next to this file):
    seeder-samples.json   raw samples (one per fault window) + counter evolution
    c2-scan.txt           controller validator + C2 scan results per sample
    stdout                the same summary
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
OUT_DIR = HERE.parent
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / "controller"))


def load_seeder():
    path = REPO / "console" / "seed_demo.py"
    spec = importlib.util.spec_from_file_location("seed_demo_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# A controller-issued, valid 26-char ULID (the shape the seeder is handed back
# from `register`).
PROJECT_ID = "01M539DJ5WDFPAH3NR9NNXXVS2"


def main() -> int:
    seed = load_seeder()
    from app import governance
    from app.validate import validate_sample

    vm_ids = [seed.vm_id_for_instance("demo-workload"),
              seed.vm_id_for_instance("demo-workload-2")]

    def build(fault, *, oom_kill, pgmajfault, tcp_retrans, io_ticks,
              inst_oom, inst_pgmaj, ram_used, disk_lat):
        return seed.sample(
            "demo-node-c", PROJECT_ID, 100,
            ram_used_bytes=ram_used, disk_write_latency_ms=disk_lat,
            latency_ms=0.8, uptime_sec=3600, vm_ids=vm_ids, fault=fault,
            oom_kill_count=oom_kill, pgmajfault_count=pgmajfault,
            tcp_retransmit_segs=tcp_retrans, io_ticks_ms=io_ticks,
            instance_oom_kill_count=inst_oom,
            instance_pgmajfault_count=inst_pgmaj)

    # One raw sample per fault window, with counters that reflect a node that has
    # already ridden a few fault windows (so the cumulative counters are nonzero).
    samples = {
        "healthy": build(
            None, oom_kill=0, pgmajfault=21000, tcp_retrans=4800,
            io_ticks=6200000, inst_oom=0, inst_pgmaj=15000,
            ram_used=18000000000, disk_lat=3.0),
        "disk-degrade": build(
            seed.FAULT_DISK, oom_kill=0, pgmajfault=21240,
            tcp_retrans=4802, io_ticks=6840000, inst_oom=0, inst_pgmaj=15000,
            ram_used=18000000000, disk_lat=120.0),
        "memory-pressure": build(
            seed.FAULT_MEMORY, oom_kill=4, pgmajfault=31800,
            tcp_retrans=4803, io_ticks=6260000, inst_oom=3, inst_pgmaj=24800,
            ram_used=66000000000, disk_lat=3.0),
    }

    # Counter evolution over a few faulted ticks, for each node type, to show the
    # counters advance only under the fault that names them.
    evolution: dict[str, list[dict]] = {"memory-node": [], "disk-node": []}

    mem = seed.NodeTicker("http://127.0.0.1:1", {"node_id": "n", "node_token": "t"},
                          PROJECT_ID, period_s=6.0, phase=0.0, high_ram=True)
    disk = seed.NodeTicker("http://127.0.0.1:1", {"node_id": "n", "node_token": "t"},
                           PROJECT_ID, period_s=6.0, phase=0.0, high_ram=False)
    for tick in range(1, 10):
        mem._advance_counters(seed.FAULT_MEMORY)
        disk._advance_counters(seed.FAULT_DISK)
        evolution["memory-node"].append({
            "tick": tick,
            "oom_kill_count": mem.oom_kills,
            "pgmajfault_count": mem.pgmajfaults,
            "instance_oom_kill_count": mem.instance_oom_kills,
            "instance_pgmajfault_count": mem.instance_pgmajfaults,
        })
        evolution["disk-node"].append({
            "tick": tick,
            "io_ticks_ms": disk.io_ticks_ms,
            "oom_kill_count": disk.oom_kills,  # stays 0: disk fault never OOMs
        })

    report: list[str] = []
    report.append("== controller validator (app.validate.validate_sample) + C2 scan ==")
    for name, sample in samples.items():
        clean, errors = validate_sample(sample)
        c2 = governance.scan_c2(clean)
        c1 = governance.check_c1_transforms(clean)
        report.append(
            f"  {name}: validate errors={errors}  "
            f"scan_c2={c2}  check_c1={c1}  clean==raw={clean == sample}")
        report.append(f"    pressure={sample['pressure']}  "
                      f"mem_available_bytes={sample['mem_available_bytes']}")
        report.append(f"    disk[0] io_ticks_ms={sample['disk'][0]['io_ticks_ms']} "
                      f"io_in_flight={sample['disk'][0]['io_in_flight']} "
                      f"write_latency_ms={sample['disk'][0]['write_latency_ms']}")
        report.append(f"    oom_kill_count={sample['oom_kill_count']} "
                      f"pgmajfault_count={sample['pgmajfault_count']} "
                      f"lxd_instances[0]={ {k: sample['lxd_instances'][0][k] for k in ('oom_kill_count', 'pgmajfault_count')} }")
    report.append("")
    report.append("== cumulative counter evolution over 9 faulted ticks ==")
    report.append("  memory node (FAULT_MEMORY): " + json.dumps(evolution["memory-node"]))
    report.append("  disk node (FAULT_DISK):     " + json.dumps(evolution["disk-node"]))

    text = "\n".join(report) + "\n"
    (OUT_DIR / "c2-scan.txt").write_text(text)
    (OUT_DIR / "seeder-samples.json").write_text(
        json.dumps({"samples": samples, "counter_evolution": evolution},
                   indent=2, sort_keys=True) + "\n")
    print(text)

    all_ok = True
    for name, sample in samples.items():
        clean, errors = validate_sample(sample)
        if errors or governance.scan_c2(clean) or governance.check_c1_transforms(clean):
            all_ok = False
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
