#!/usr/bin/env python3
"""Register a node with a running controller and feed it a REAL telemetry sample.

The dashboard half of the metric expansion (task t_8b60acb2) must be evidenced
against a real sample, not a synthesised one. The collector card committed one:
`docs/evidence/2026-10-16-metric-expansion/node-payload.json` — a real
`build_sample()` from the build host, copied into `../sample/` beside this tool.

This script does exactly what a node does, with no stub anywhere:

    POST /v1/nodes/register            (bootstrap token, single use)
    POST /v1/nodes/{node_id}/telemetry (node token)   <- the committed payload,
                                        wrapped in the frozen batch envelope

so the sample passes the REAL governance gate (C2 scan, C1 transform check,
redactor version), the REAL schema validation and the REAL store, and the
dashboard then reads it back as `metrics_latest` (dashboard.md §1.2).

It then posts a SECOND sample to the same node: the same payload with every
§4.12–§4.14 field removed — the shape the collector emits when its sources are
unreadable (its omit-not-fabricate rule), and the input the dashboard must render
as "not reported" rather than as a flat zero. That file is derived here (never
hand-written) and written beside the captures so the derivation is auditable.

The node credential is written to a 0600 file and never printed.

Usage:
    post_real_payload.py --base http://127.0.0.1:PORT --bootstrap-token T \\
        --project-id 01... --project-token T --vm-id 01... \\
        --payload ../sample/node-payload.json --node-out /path/work/node.json \\
        --capture-dir /path/capture
Prints: {"node_id": ..., "real": {...}, "absent": {...}}
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
import pathlib
import stat
import sys
import time
import urllib.error
import urllib.request

_HERE = pathlib.Path(__file__).resolve()


def _repo_root() -> pathlib.Path:
    for parent in _HERE.parents:
        if (parent / "chaos" / "node_sim.py").is_file():
            return parent
    raise SystemExit("cannot locate the repo root (no chaos/node_sim.py above this file)")


sys.path.insert(0, str(_repo_root()))

from chaos import node_sim  # noqa: E402

# Every §4.12–§4.14 field, by contract section. Dropping all of them produces the
# "sources unreadable" sample the panel must render as NOT REPORTED.
EXPANSION_SCALARS = [
    "pressure", "oom_kill_count", "pgmajfault_count", "mem_available_bytes",
    "tcp_retransmit_segs", "conntrack_count", "conntrack_max",
]
EXPANSION_DISK_FIELDS = ["io_ticks_ms", "io_in_flight", "inode_used", "inode_total"]
EXPANSION_NET_FIELDS = ["rx_errors", "tx_errors", "rx_dropped", "tx_dropped"]
EXPANSION_INSTANCE_FIELDS = ["oom_kill_count", "pgmajfault_count"]


def drop_expansion(sample: dict) -> dict:
    """The same sample as a node that could not read any §4.12–§4.14 source."""
    out = copy.deepcopy(sample)
    for key in EXPANSION_SCALARS:
        out.pop(key, None)
    for entry in out.get("disk") or []:
        for f in EXPANSION_DISK_FIELDS:
            entry.pop(f, None)
    for entry in out.get("net") or []:
        for f in EXPANSION_NET_FIELDS:
            entry.pop(f, None)
    for entry in out.get("lxd_instances") or []:
        for f in EXPANSION_INSTANCE_FIELDS:
            entry.pop(f, None)
    return out


def http(method: str, url: str, body=None, token: str | None = None, timeout: float = 15.0):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "X-Termini-Version": "1"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode(errors="replace")}


def later_ts(ts: str, seconds: int = 60) -> str:
    """A ts strictly after the sample's own, so it becomes the LATEST sample (§1.2)."""
    base = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    return (base + datetime.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def expansion_keys(sample: dict) -> list[str]:
    """Which §4.12–§4.14 fields the stored sample actually carries (to print)."""
    present = [k for k in EXPANSION_SCALARS if k in sample]
    for entry in sample.get("disk") or []:
        present += [f"disk[].{f}" for f in EXPANSION_DISK_FIELDS if f in entry]
    for entry in sample.get("net") or []:
        present += [f"net[].{f}" for f in EXPANSION_NET_FIELDS if f in entry]
    for entry in sample.get("lxd_instances") or []:
        present += [f"lxd_instances[].{f}" for f in EXPANSION_INSTANCE_FIELDS if f in entry]
    return present


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True)
    ap.add_argument("--bootstrap-token", default="")
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--project-token", required=True)
    ap.add_argument("--vm-id", default="")
    ap.add_argument("--payload", required=True)
    ap.add_argument("--hostname", default="evidence-metric-node")
    ap.add_argument("--node-out", required=True)
    ap.add_argument("--node-in", default="", help="existing node credential file (--stage absent)")
    ap.add_argument("--stage", choices=["real", "absent", "both"], default="both",
                    help="which sample to post; the capture takes 01 between the two stages")
    ap.add_argument("--capture-dir", required=True)
    args = ap.parse_args(argv)

    base = args.base.rstrip("/")
    payload = json.loads(pathlib.Path(args.payload).read_text())
    capdir = pathlib.Path(args.capture_dir)
    capdir.mkdir(parents=True, exist_ok=True)

    if args.stage == "absent":
        existing = json.loads(pathlib.Path(args.node_in).read_text())
        node_id, node_token = existing["node_id"], existing["node_token"]
    else:
        if not args.bootstrap_token or not args.vm_id:
            raise SystemExit("--bootstrap-token and --vm-id are required to register a node")
        status, reg = http("POST", f"{base}/v1/nodes/register",
                           node_sim.build_registration(args.hostname, gpu=False), args.bootstrap_token)
        if status not in (200, 201) or not isinstance(reg, dict) or not reg.get("node_id"):
            raise SystemExit(f"registration failed: HTTP {status} {reg}")
        node_id = reg["node_id"]
        node_token = reg.get("node_token")
        if not node_token:
            raise SystemExit("registration returned no node_token")

    def post(sample: dict, label: str):
        body = node_sim.build_batch(node_id, sample)
        st, resp = http("POST", f"{base}/v1/nodes/{node_id}/telemetry", body, node_token)
        (capdir / f"{label}-ingest-response.json").write_text(json.dumps(resp, indent=2) + "\n")
        if st != 202:
            raise SystemExit(f"telemetry rejected ({label}): HTTP {st} {resp}")
        return resp

    def wait_stored(seq: int, label: str, timeout_s: float = 30.0):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            st, recent = http("GET", f"{base}/v1/nodes/{node_id}/telemetry/recent?limit=5", None, node_token)
            if st == 200:
                seqs = [s.get("seq") for s in (recent.get("samples") or [])]
                if seq in seqs:
                    (capdir / f"{label}-node-recent.json").write_text(json.dumps(recent, indent=2) + "\n")
                    return seqs
            time.sleep(0.4)
        raise SystemExit(f"the controller never stored seq={seq} ({label})")

    def node_detail(label: str) -> dict:
        """Exactly what the dashboard fetches (dashboard.md §1.2), as the project."""
        st, detail = http("GET", f"{base}/v1/projects/{args.project_id}/nodes/{node_id}", None, args.project_token)
        if st != 200 or not isinstance(detail, dict):
            raise SystemExit(f"node detail failed ({label}): HTTP {st} {detail}")
        (capdir / f"{label}-node-detail.json").write_text(json.dumps(detail, indent=2) + "\n")
        return detail

    # ---- 1. the REAL committed payload --------------------------------------
    real = payload
    if args.stage in ("real", "both"):
        real_resp = post(real, "01-real-sample")
        seqs_real = wait_stored(int(real["seq"]), "01-real-sample")
        detail_real = node_detail("01-real-sample")
        latest_real = detail_real.get("metrics_latest") or {}
    else:
        real_resp, seqs_real, latest_real = None, None, None

    # The node credential is written before the second stage so a `--stage absent`
    # re-run can reach the same node without re-registering (a bootstrap token is
    # single use, and a new node would not be the node the panel is showing).
    out = pathlib.Path(args.node_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"node_id": node_id, "node_token": node_token,
                               "vm_id": args.vm_id, "project_id": args.project_id}))
    out.chmod(stat.S_IRUSR | stat.S_IWUSR)

    # ---- 2. the same sample with every §4.12–§4.14 field dropped -------------
    absent_resp, seqs_absent, latest_absent = None, None, None
    if args.stage in ("absent", "both"):
        absent = drop_expansion(real)
        absent["seq"] = int(real["seq"]) + 1
        absent["ts"] = later_ts(real["ts"], 60)
        (capdir / "sample-no-expansion-fields.json").write_text(json.dumps(absent, indent=2, sort_keys=True) + "\n")
        absent_resp = post(absent, "02-absent-not-zero")
        seqs_absent = wait_stored(int(absent["seq"]), "02-absent-not-zero")
        detail_absent = node_detail("02-absent-not-zero")
        latest_absent = detail_absent.get("metrics_latest") or {}

    print(json.dumps({
        "node_id": node_id,
        "real": None if latest_real is None else {
            "ingest": real_resp,
            "stored_seqs": seqs_real,
            "latest_seq": latest_real.get("seq"),
            "expansion_fields": sorted(expansion_keys(latest_real)),
        },
        "absent": None if latest_absent is None else {
            "ingest": absent_resp,
            "stored_seqs": seqs_absent,
            "latest_seq": latest_absent.get("seq"),
            "expansion_fields": sorted(expansion_keys(latest_absent)),
        },
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
