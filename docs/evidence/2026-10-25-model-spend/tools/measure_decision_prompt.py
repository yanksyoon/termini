#!/usr/bin/env python3
"""Measure the REAL prompt size of the operator decision call, in Vertex tokens.

This renders the payload with the repo's own shipped renderer
(`termini.decision.prompt.build_messages`) and asks Vertex's free `:countTokens`
endpoint how many tokens it is — so the number is what the provider bills, not a
character-count estimate.

Why it matters: the operator's evidence window is a list of full telemetry
snapshots, so the prompt's cost scales with the window size, and the window is a
config knob (`TERMINI_OPERATOR_EVIDENCE_SAMPLES`). This tool is how that knob's
price is known rather than assumed.

Usage:
    python3 measure_decision_prompt.py [--project P] [--samples 20 6 1]

Requires an authenticated `gcloud` with `aiplatform.endpoints.predict` on the
project (the same permission the decision call itself needs).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from termini.decision import prompt as P  # noqa: E402
from termini.decision.types import ACTION_SPACE, DecisionInput  # noqa: E402

RAM_TOTAL = 17_179_869_184


def sample(seq: int, ram_used: int, oom: int) -> dict:
    """One node-telemetry §4 sample, in the shape the controller stores."""
    return {
        "seq": seq, "ts": f"2026-10-19T09:{seq % 60:02d}:00.000Z", "uptime_sec": 86400 + seq,
        "cpu_util_pct": 41.2, "cpu_core_count": 4,
        "ram_total_bytes": RAM_TOTAL, "ram_used_bytes": ram_used, "swap_used_bytes": 0,
        "load_avg_1m": 2.31, "cpu_temp_celsius": 51.0,
        "os_name": "Ubuntu", "os_version": "24.04", "kernel_version": "6.8.0-142-generic",
        "lxd_cluster_role": "member", "hostname": "5f2a9c1e7b4d4a1f",
        "ip_local": "10.10.0.11/24", "mac_address": "9c1e7b4d4a1f5f2a",
        "project_id": "01M4349RPSZFTB60AVJEG0ZZY9",
        "pressure": {"cpu_some_avg10": 12.4, "io_some_avg10": 3.1,
                     "memory_some_avg10": 58.2, "memory_full_avg10": 31.0},
        "oom_kill_count": oom, "pgmajfault_count": 190000 + seq,
        "tcp_retransmit_segs": 42 + seq,
        "conntrack_count": 1200, "conntrack_max": 262144,
        "mem_available_bytes": RAM_TOTAL - ram_used,
        "disk": [{"path": "/dev/sda", "total_bytes": 274_877_906_944,
                  "free_bytes": 214_748_364_800, "write_latency_ms": 3.0,
                  "mount_ro": False, "io_ticks_ms": 991000, "io_in_flight": 0,
                  "inode_used": 81234, "inode_total": 65536000}],
        "net": [{"iface": "eth0", "rx_bytes": 123000 + seq, "tx_bytes": 11000 + seq,
                 "latency_ms": 0.8, "rx_errors": 0, "tx_errors": 0,
                 "rx_dropped": 0, "tx_dropped": 0}],
        "gpu": [],
        "lxd_instances": [
            {"vm_id": "01M4349RPXWDC52ZDAVMPCMJSH", "instance_name": "clinic-agent",
             "state": "running", "cpu_util_pct": 12.0, "ram_used_bytes": 14_000_000_000,
             "ram_limit_bytes": 15_032_385_536, "disk_used_bytes": 2_362_232_012,
             "state_age_sec": 812, "restart_count": 1,
             "oom_kill_count": oom, "pgmajfault_count": 190000 + seq},
            {"vm_id": "01M4349RPXWDC52ZDAVMPCMJS2", "instance_name": "worker",
             "state": "running", "cpu_util_pct": 4.0, "ram_used_bytes": 900_000_000,
             "ram_limit_bytes": 1_073_741_824, "disk_used_bytes": 512_000_000,
             "state_age_sec": 400, "restart_count": 0},
        ],
    }


def build(n_samples: int) -> DecisionInput:
    ev = [sample(1030 + i, RAM_TOTAL - 1_000_000_000 + i * 9_000_000, 3 + i // 6)
          for i in range(n_samples)]
    latest = ev[-1]
    return DecisionInput(
        request_id="01M8COSTMEASURE0000000000",
        project_id="01M4349RPSZFTB60AVJEG0ZZY9",
        node_id="01M4349RPTT29RNTC6VBCXQ0RC",
        rule_id="oom-guard-01",
        type="oom",
        trigger={"sample_seq": latest["seq"], "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
                 "metric": "ram_used_bytes", "value": latest["ram_used_bytes"],
                 "threshold": ">= 95% of ram_total_bytes for 2 consecutive samples",
                 "rule_id": "oom-guard-01", "fired_at": "2026-10-19T09:29:05Z"},
        state=latest,
        evidence=ev,
        target_resource={"node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
                         "instances": latest["lxd_instances"]},
        allowed_action_space=[{"action_id": a, "params_schema": s.get("params", {})}
                              for a, s in ACTION_SPACE.items()],
        derived={"window_samples": len(ev), "oom_kill_count_delta": 3,
                 "pgmajfault_count_delta": 412, "tcp_retransmit_segs_delta": 19},
    )


def count_tokens(project: str, messages: list, model: str = "gemini-2.5-flash") -> int:
    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    contents: list = []
    for m in messages:
        if m["role"] == "system":
            continue
        role = "model" if m["role"] == "assistant" else "user"
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].append({"text": m["content"]})
        else:
            contents.append({"role": role, "parts": [{"text": m["content"]}]})
    payload: dict = {"contents": contents}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    token = subprocess.run(["gcloud", "auth", "print-access-token"],
                           capture_output=True, text=True, check=True).stdout.strip()
    url = (f"https://aiplatform.googleapis.com/v1/projects/{project}/locations/global"
           f"/publishers/google/models/{model}:countTokens")
    out = subprocess.run(
        ["curl", "-sS", "-m", "60", "-X", "POST", url,
         "-H", "Authorization: Bearer " + token,
         "-H", "Content-Type: application/json", "-d", json.dumps(payload)],
        capture_output=True, text=True, check=True)
    return int(json.loads(out.stdout)["totalTokens"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--samples", type=int, nargs="+", default=[20, 6, 1])
    args = ap.parse_args()
    for n in args.samples:
        total = count_tokens(args.project, P.build_messages(build(n)))
        print(f"evidence_samples={n:>2}  prompt_tokens={total:>7,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
