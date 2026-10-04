#!/usr/bin/env python3
"""End-to-end acceptance test for the Termini Controller core.

Boots the controller on an ephemeral port with a temp DB, then exercises every
acceptance criterion from the task card:

  1. register -> deterministic role; a second node with different capabilities
     yields the other role
  2. telemetry at 1s cadence for >=2 nodes is accepted and queryable, with
     timestamps preserved
  3. malformed telemetry is rejected 422 and does not corrupt stored data
  4. docs/telemetry-schema.md exists and matches the enforced schema

Usage: python3 scripts/acceptance.py   (run from anywhere)
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

CONTROLLER_DIR = pathlib.Path(__file__).resolve().parent.parent
REPO_ROOT = CONTROLLER_DIR.parent
SCHEMA_DOC = REPO_ROOT / "docs" / "telemetry-schema.md"
sys.path.insert(0, str(CONTROLLER_DIR))

from app import fields as F  # noqa: E402
from app import ulid  # noqa: E402

PORT = int(os.environ.get("TERMINI_TEST_PORT", "18080"))
BOOT_A = "test-bootstrap-a"
BOOT_C = "test-bootstrap-c"
BOOT_C2 = "test-bootstrap-c2"
PROJECT_TOKEN = "test-project-token"

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def http(method: str, path: str, body=None, token: str | None = None, base: str = "") -> tuple[int, object]:
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
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


def node_reg(machine_id: str, hostname: str, gpus: list) -> dict:
    return {
        "schema_version": "1.0",
        "machine": {
            "machine_id": machine_id,
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
        "host_ssh_keys": ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI root@" + hostname],
        "agent": {"version": "0.1.0", "boot_id": "8f0e"},
    }


L4_24GB = 24 * 1024 ** 3  # 25769803776


def telemetry_batch(node_id: str, seq: int, ts: str, *, gpu: bool = False) -> dict:
    sample = {
        "seq": seq,
        "ts": ts,
        "uptime_sec": 3600 + seq,
        "cpu_util_pct": 31.2,
        "cpu_core_count": 16,
        "ram_total_bytes": 34359738368,
        "ram_used_bytes": 19756849561,
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
        "disk": [
            {"path": "/dev/sda", "total_bytes": 274877906944, "free_bytes": 214748364800,
             "write_latency_ms": 3.0, "mount_ro": False}
        ],
        "net": [{"iface": "eth0", "rx_bytes": 123000, "tx_bytes": 11000, "latency_ms": 0.8}],
    }
    if gpu:
        sample["gpu"] = [
            {"model": "NVIDIA L4", "util_pct": 88.0, "vram_total_bytes": L4_24GB,
             "vram_used_bytes": 12884901888, "temp_celsius": 62.0}
        ]
    return {
        "_redaction": {"version": "redactor-config v3", "config_hash": "3f2a1b", "result": "ok"},
        "call_id": ulid.new_ulid(),
        "node_id": node_id,
        "schema_version": 1,
        "governance_chain_tip": ulid.new_ulid(),
        "samples": [sample],
    }


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="termini-acceptance-")
    db_path = os.path.join(tmp, "test.db")

    env = dict(os.environ)
    env.update({
        "TERMINI_HOST": "127.0.0.1",
        "TERMINI_PORT": str(PORT),
        "TERMINI_DB_PATH": db_path,
        "TERMINI_BOOTSTRAP_TOKEN": f"{BOOT_A},{BOOT_C},{BOOT_C2}",
        "TERMINI_PROJECT_TOKEN": PROJECT_TOKEN,
        "TERMINI_MAX_INFERENCE_NODES": "1",
        "TERMINI_MIN_REDACTOR_VERSION": "3",
        "PYTHONUNBUFFERED": "1",
    })

    proc = subprocess.Popen(
        [sys.executable, "-m", "app.main"],
        cwd=str(CONTROLLER_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{PORT}"

    ready = False
    for _ in range(100):
        try:
            http("GET", "/v1/nodes/register", base=base)
            ready = True
            break
        except Exception:
            time.sleep(0.1)
    check("controller boots and serves HTTP", ready)
    if not ready:
        proc.terminate()
        out, _ = proc.communicate(timeout=3)
        print(out.decode(errors="replace"))
        return 1

    project_id = ""

    try:
        # ------------------------------------------------------------- 1. roles
        st, r = http("POST", "/v1/nodes/register", node_reg("m-a", "node-a", []), token=BOOT_A, base=base)
        check("register node A (no GPU) -> 201", st == 201, f"status={st} body={r}")
        check("node A role == compute", isinstance(r, dict) and r.get("role") == "compute", str(r))
        node_a_id = r.get("node_id") if isinstance(r, dict) else None
        node_a_token = r.get("node_token") if isinstance(r, dict) else None
        project_id = r.get("project_id") if isinstance(r, dict) else ""
        check("node A node_id is ULID", isinstance(node_a_id, str) and ulid.is_valid_ulid(node_a_id), str(node_a_id))
        check("node A node_token issued", isinstance(node_a_token, str) and len(node_a_token) >= 32)

        st, r = http("POST", "/v1/nodes/register",
                     node_reg("m-c", "node-c", [{"vendor": "nvidia", "model": "NVIDIA L4",
                                                 "vram_bytes": L4_24GB, "health": "ok"}]),
                     token=BOOT_C, base=base)
        check("register node C (GPU 24GB) -> 201", st == 201, f"status={st} body={r}")
        check("node C role == inference", isinstance(r, dict) and r.get("role") == "inference", str(r))
        node_c_id = r.get("node_id") if isinstance(r, dict) else None
        node_c_token = r.get("node_token") if isinstance(r, dict) else None

        st, r = http("POST", "/v1/nodes/register",
                     node_reg("m-c2", "node-c2", [{"vendor": "nvidia", "model": "NVIDIA L4",
                                                   "vram_bytes": L4_24GB, "health": "ok"}]),
                     token=BOOT_C2, base=base)
        check("register second GPU node -> 201", st == 201, f"status={st}")
        check("second GPU node capped to compute", isinstance(r, dict) and r.get("role") == "compute", str(r))

        st, r = http("POST", "/v1/nodes/register", node_reg("m-x", "node-x", []), token=BOOT_A, base=base)
        check("bootstrap token reuse -> 401", st == 401, f"status={st}")

        # ------------------------------------------------------------- 2. list
        st, r = http("GET", f"/v1/projects/{project_id}/nodes", token=PROJECT_TOKEN, base=base)
        check("GET /nodes -> 200", st == 200, f"status={st}")
        nodes = r.get("nodes", []) if isinstance(r, dict) else []
        roles = {n["node_id"]: n["role"] for n in nodes}
        check("list shows >= 3 nodes", len(nodes) >= 3, str(len(nodes)))
        check("list has role + last_seen_at", all("role" in n and "last_seen_at" in n for n in nodes))
        check("list reflects assigned roles",
              bool(roles.get(node_a_id) == "compute" and roles.get(node_c_id) == "inference"), str(roles))

        # ------------------------------------------------------------- 3. telemetry
        good_ts_a = []
        for i in range(3):  # 1s cadence
            ts = f"2026-10-04T07:49:0{i}.000Z"
            batch = telemetry_batch(node_a_id, 1000 + i, ts, gpu=False)
            st, r = http("POST", f"/v1/nodes/{node_a_id}/telemetry", batch, token=node_a_token, base=base)
            check(f"telemetry node A sample {i} -> 202", st == 202, f"status={st} body={r}")
            if st == 202:
                good_ts_a.append(ts)
            time.sleep(1.0)

        for i in range(3):
            ts = f"2026-10-04T07:50:0{i}.000Z"
            batch = telemetry_batch(node_c_id, 2000 + i, ts, gpu=True)
            st, r = http("POST", f"/v1/nodes/{node_c_id}/telemetry", batch, token=node_c_token, base=base)
            check(f"telemetry node C sample {i} -> 202", st == 202, f"status={st} body={r}")
            time.sleep(1.0)

        # ------------------------------------------------------- queryable + timestamps
        st, r = http("GET", f"/v1/telemetry/recent?node={node_a_id}&limit=10", token=PROJECT_TOKEN, base=base)
        check("GET /v1/telemetry/recent -> 200", st == 200, f"status={st}")
        samples = r.get("samples", []) if isinstance(r, dict) else []
        check("recent returns >= 3 samples", len(samples) >= 3, str(len(samples)))
        ts_seen = {s.get("ts") for s in samples if isinstance(s, dict)}
        check("timestamps preserved verbatim", bool(good_ts_a and set(good_ts_a) <= ts_seen),
              f"expected {good_ts_a} in {ts_seen}")

        st, r = http("GET", f"/v1/projects/{project_id}/nodes/{node_c_id}/metrics"
                     "?resolution=raw&metrics=gpu_util_pct,cpu_util_pct",
                     token=PROJECT_TOKEN, base=base)
        check("GET metrics -> 200", st == 200, f"status={st}")
        series = {s["name"]: s for s in r.get("series", [])} if isinstance(r, dict) else {}
        check("gpu_util_pct series present",
              bool("gpu_util_pct" in series and series["gpu_util_pct"]["points"]), str(series.keys()))
        check("gpu series unit is percent", series.get("gpu_util_pct", {}).get("unit") == "percent")

        # ------------------------------------------------------- 4. malformed -> 422, no corruption
        def recent_count() -> int:
            _, rr = http("GET", f"/v1/telemetry/recent?node={node_a_id}&limit=100", token=PROJECT_TOKEN, base=base)
            return len(rr.get("samples", [])) if isinstance(rr, dict) else 0

        good = telemetry_batch(node_a_id, 1005, "2026-10-04T07:51:00.000Z")
        before = recent_count()
        cases = [
            ("cpu_util_pct out of range", {**good, "samples": [{**good["samples"][0], "cpu_util_pct": 250}]}),
            ("cpu_util_pct wrong type", {**good, "samples": [{**good["samples"][0], "cpu_util_pct": "abc"}]}),
            ("missing ts", {**good, "samples": [{k: v for k, v in good["samples"][0].items() if k != "ts"}]}),
            ("raw ip_local (C1 untransformed)", {**good, "samples": [{**good["samples"][0], "ip_local": "10.0.0.12"}]}),
        ]
        for name, bad in cases:
            st, r = http("POST", f"/v1/nodes/{node_a_id}/telemetry", bad, token=node_a_token, base=base)
            check(f"malformed ({name}) -> 422", st == 422, f"status={st} body={r}")

        after = recent_count()
        check("no data corruption after malformed", after == before, f"before={before} after={after}")

        st, r = http("POST", f"/v1/nodes/{node_a_id}/telemetry",
                     {**good, "samples": [{**good["samples"][0], "prompt": "x"}]},
                     token=node_a_token, base=base)
        check("C2 content -> 422 GOVERNANCE_REJECT",
              st == 422 and isinstance(r, dict) and r.get("error", {}).get("code") == "GOVERNANCE_REJECT", str(r))

        # ------------------------------------------------------- 4. schema doc matches
        check("docs/telemetry-schema.md exists", SCHEMA_DOC.exists(), str(SCHEMA_DOC))
        doc_text = SCHEMA_DOC.read_text() if SCHEMA_DOC.exists() else ""
        missing = [k for k in F.SAMPLE_FIELDS if k not in doc_text]
        check("schema doc covers all sample fields", not missing, f"missing from doc: {missing}")
        missing_cont = [k for k in F.SAMPLE_CONTAINERS if k not in doc_text]
        check("schema doc covers all container fields", not missing_cont, f"missing from doc: {missing_cont}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
