#!/usr/bin/env python3
"""Register ONE node with a running controller and give it a real telemetry sample.

The demo-fault injector refuses to inject onto a node it cannot find, and builds
the "healthy" side of the injected series from the node's own latest sample (or
its hardware facts). This script therefore does what a real node does: it
registers (bootstrap token), stores its node credential (0600, never printed),
and posts one healthy sample through `POST /v1/nodes/{nid}/telemetry` — so the
fault series the controller later writes is a *superset of this node's own
telemetry*, including the `lxd_instances[]` entry the operator's `heaviest`
target resolution reads.

Usage:
    node_setup.py --base http://127.0.0.1:PORT --bootstrap-token T \
                  --project-id 01... --vm-id 01... --out /path/work/node.json
Prints: {"node_id": "...", "vm_id": "...", "samples": N}
"""
from __future__ import annotations

import argparse
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True)
    ap.add_argument("--bootstrap-token", required=True)
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--vm-id", required=True, help="the LXD vm_id the sample reports")
    ap.add_argument("--hostname", default="evidence-node-a")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    base = args.base.rstrip("/")
    status, reg = http("POST", f"{base}/v1/nodes/register",
                       node_sim.build_registration(args.hostname, gpu=False), args.bootstrap_token)
    if status not in (200, 201) or not isinstance(reg, dict) or not reg.get("node_id"):
        raise SystemExit(f"registration failed: HTTP {status} {reg}")
    node_id = reg["node_id"]
    node_token = reg.get("node_token")
    if not node_token:
        raise SystemExit("registration returned no node_token")

    ram_total = int(reg.get("ram_bytes") or 68719476736)
    disk_total = int(reg.get("disk_bytes") or 274877906944)
    node = {
        "ram_total_bytes": ram_total,
        "disk_total_bytes": disk_total,
        "gpu": False,
        "cpu_core_count": int(reg.get("cpu_cores") or 16),
    }
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    sample = node_sim.generate_sample(
        node=node, project_id=args.project_id, vm_id=args.vm_id,
        mode="healthy", params={}, seq=1, elapsed_s=0.0, ts=ts,
    )
    status, body = http("POST", f"{base}/v1/nodes/{node_id}/telemetry",
                        node_sim.build_batch(node_id, sample), node_token)
    if status not in (200, 202):
        raise SystemExit(f"telemetry rejected: HTTP {status} {body}")

    # wait until the controller really holds the sample: the injector's baseline
    # is read from the store, so this must be durable before a fault is injected.
    stored = 0
    for _ in range(40):
        st, recent = http("GET", f"{base}/v1/nodes/{node_id}/telemetry/recent?limit=5", None, node_token)
        if st == 200 and isinstance(recent, dict):
            stored = len(recent.get("samples") or [])
        if stored >= 1:
            break
        time.sleep(0.5)
    if stored < 1:
        raise SystemExit("the controller stored no sample for the node")

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"node_id": node_id, "node_token": node_token,
                               "vm_id": args.vm_id, "project_id": args.project_id}))
    out.chmod(stat.S_IRUSR | stat.S_IWUSR)
    # the credential stays in the 0600 file only
    print(json.dumps({"node_id": node_id, "vm_id": args.vm_id, "samples": stored}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
