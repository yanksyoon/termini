#!/usr/bin/env python3
"""Drive the deployed controller's plan lifecycle for evidence capture.

Run ON the controller VM (stdlib only). Subcommands:

    claim-gb.py select            # intent(auto) -> which node resolved?  (claim g)
    claim-gb.py apply  <plan_id>  # materialise the plan                (claim b)
    claim-gb.py release <plan_id> # release its committed capacity
    claim-gb.py unapply <plan_id> # apply, then release
    claim-gb.py cycles <N>        # apply+release N times, report capacity each round (claim f)

Every step prints the raw HTTP status + body, so the transcript is the evidence.
"""
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("TERMINI_BASE", "http://127.0.0.1:8080")
PID = os.environ.get("TERMINI_PROJECT_ID", "01J0DEM0WARM000000000000AB")
TOKEN = os.environ.get("TERMINI_PROJECT_TOKEN", "demo-project-token")
DB = os.environ.get("TERMINI_DB_PATH", "/data/controller.db")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=float(os.environ.get("TERMINI_HTTP_TIMEOUT","180"))) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw.decode(errors="replace")


def q(sql, args=()):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def live_capacity():
    r = q("select coalesce(sum(cpu_allocated),0) c, coalesce(sum(ram_allocated_gb),0) r,"
          " count(*) n from vm_allocations where released_at is null")[0]
    return r


def node_of(nid):
    rows = q("select id,registration_id,role,synthetic,lxd_endpoint from nodes where id=?", (nid,))
    return rows[0] if rows else None


def intent(count=2):
    spec = {"spec": {"allocations": [
        {"type": "agent_vm", "count": count, "vcpus": 1, "ram_gb": 1, "disk_gb": 8,
         "target_node": os.environ.get("TERMINI_TARGET", "auto")}]}}
    st, body = call("POST", f"/v1/projects/{PID}/intents", spec)
    print(f"POST /intents -> HTTP {st}")
    print(json.dumps(body, indent=1)[:1600])
    return st, body


def cmd_select():
    st, plan = intent(2)
    if st != 200:
        print("INTENT FAILED"); return 1
    pid = plan["plan_id"]
    alloc = plan["allocations"][0]
    nid = alloc["node_id"]
    n = node_of(nid)
    print(f"\nPLAN {pid}: allocation resolved to node_id={nid}")
    print(f"  that node is: registration_id={n['registration_id']} role={n['role']} "
          f"synthetic={n['synthetic']} lxd_endpoint={n['lxd_endpoint']}")
    for r in q("select id,registration_id,synthetic,json_extract(hardware_json,'$.memory.total_bytes') ram"
               " from nodes where decommissioned_at is null and role in ('compute','inference')"):
        print(f"  candidate: {r['registration_id'][:22]:22} synthetic={r['synthetic']} ram_bytes={r['ram']}")
    print("\nVERDICT:", "REAL node chosen"
          if not n["synthetic"] else "SYNTHETIC node chosen (FAIL)")
    return 0


def cmd_apply(pid):
    st, body = call("POST", f"/v1/projects/{PID}/vms/apply", {"plan_id": pid})
    print(f"POST /vms/apply {pid} -> HTTP {st}")
    print(json.dumps(body, indent=1))
    return 0 if st in (200, 202) else 1


def cmd_release(pid, reason="evidence_release"):
    st, body = call("POST", f"/v1/projects/{PID}/vms/release", {"plan_id": pid, "reason": reason})
    print(f"POST /vms/release {pid} -> HTTP {st}")
    print(json.dumps(body, indent=1))
    return 0


def cmd_unapply(pid):
    cmd_apply(pid)
    cmd_release(pid)
    return 0


def cmd_reconcile():
    st, body = call("POST", f"/v1/projects/{PID}/vms/reconcile", {})
    print(f"POST /vms/reconcile -> HTTP {st}")
    print(json.dumps(body, indent=1))
    return 0


def cmd_cycles(n):
    n = int(n)
    print(f"live capacity at start: {live_capacity()}")
    for i in range(n):
        st, plan = intent(2)
        if st != 200:
            print(f"round {i}: INTENT FAILED HTTP {st}: {json.dumps(plan)[:300]}")
            return 1
        pid = plan["plan_id"]
        st2, applied = call("POST", f"/v1/projects/{PID}/vms/apply", {"plan_id": pid})
        st3, rel = call("POST", f"/v1/projects/{PID}/vms/release",
                        {"plan_id": pid, "reason": f"cycle_{i}"})
        print(f"round {i}: plan={pid} apply={st2} {applied.get('state') if isinstance(applied, dict) else applied} "
              f"vm_ids={applied.get('vm_ids') if isinstance(applied, dict) else None} "
              f"release={st3} {rel.get('status') if isinstance(rel, dict) else rel} "
              f"| live={live_capacity()}")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "select"
    if cmd == "select":
        sys.exit(cmd_select())
    if cmd == "apply":
        sys.exit(cmd_apply(sys.argv[2]))
    if cmd == "release":
        sys.exit(cmd_release(sys.argv[2]))
    if cmd == "unapply":
        sys.exit(cmd_unapply(sys.argv[2]))
    if cmd == "reconcile":
        sys.exit(cmd_reconcile())
    if cmd == "cycles":
        sys.exit(cmd_cycles(sys.argv[2]))
    sys.exit(f"unknown subcommand {cmd}")
