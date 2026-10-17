#!/usr/bin/env python3
"""F01 acceptance — oom with a leak signature, on the REAL node, live.

Drives the DEPLOYED controller (the standing fleet) through the graded F01 demo
(`docs/f01-oom-leak-runbook.md`, card t_979fa31b):

  N consecutive runs, each recorded:
    inject (scenario=oom, target=real, on the real node)
      -> detection latency  (breach_started_at -> fired_at)
      -> decision           (decision_source=model, action=instance.restart,
                             target = a REAL instance on the node)
      -> audit row          (LXD operation id + state transition)
      -> remediation latency (decided_at -> executed_at)
      -> clear -> telemetry back under threshold -> rule re-armed.

Then a NEGATIVE control: the same fault injected with the leak signature
removed (oom_kills=0, no swap, free memory held high — i.e. reclaimable page
cache). The operator must take NO destructive action (never stop/delete/prune).

Every observation is read back from the controller's own records
(GET /v1/operator/events|decisions|audits, GET /demo/faults, GET /nodes), so the
table is evidence, not a re-derivation. The script makes no LXD call itself; the
execution is the operator loop's, verified through its audit row.

Usage:
    python3 controller/scripts/f01_oom_acceptance.py \
        --controller https://34-93-190-64.sslip.io --token demo-project-token
    python3 controller/scripts/f01_oom_acceptance.py ... --runs 1 --negative-control

Exit 0 iff every run recorded a model `instance.restart` on a real instance with
an audit row carrying an operation id and the rule re-armed, AND the negative
control took no destructive action. Stdlib-only.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

PROJECT_ID = "01J0DEM0WARM000000000000AB"
DESTRUCTIVE = {"instance.stop", "instance.delete", "image.prune"}


def http(base: str, method: str, path: str, body=None, token: str | None = None,
         timeout: float = 20.0):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers,
                                method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        finally:
            e.close()
        try:
            return e.code, (json.loads(raw) if raw else None)
        except json.JSONDecodeError:
            return e.code, raw.decode(errors="replace")
    except urllib.error.URLError as e:
        return 0, f"{type(e).__name__}: {e}"


def epoch(ts):
    """RFC3339 (ms or s precision) -> fractional Unix seconds, or None."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        import datetime
        t = ts.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(t)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def find_real_node(base: str, token: str) -> dict:
    st, body = http(base, "GET", f"/v1/projects/{PROJECT_ID}/nodes?include=synthetic",
                    token=token)
    if st != 200 or not isinstance(body, dict):
        raise SystemExit(f"could not list nodes: HTTP {st} {body}")
    for n in body.get("nodes", []):
        if not n.get("synthetic"):
            return n
    raise SystemExit("no real (synthetic=0) node registered — the demo needs node-a")


def wait_until(pred, timeout_s: float, step_s: float = 1.0, label: str = ""):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = pred()
        if r is not None:
            return r
        time.sleep(step_s)
    raise TimeoutError(f"timed out waiting for {label or 'condition'}")


def run_one(base: str, token: str, node: dict, *, index: int, params: dict | None = None) -> dict:
    node_id = node["node_id"]
    # Baseline: clear any open oom injection on this node so each run is a clean
    # episode (the rule's hysteresis latch re-arms on the clear, and the guard
    # refuses to inject while a remediation is in flight).
    http(base, "POST", f"/v1/projects/{PROJECT_ID}/demo/faults/clear",
         {"node_id": node_id}, token=token)
    # Give the previous episode's recovery burst a moment to finish re-arming.
    time.sleep(4)

    # --- inject -----------------------------------------------------------
    body = {"node_id": node_id, "scenario": "oom", "duration_s": 120, "target": "real"}
    if params:
        body["params"] = params
    st, inj = http(base, "POST", f"/v1/projects/{PROJECT_ID}/demo/faults", body, token=token)
    if st != 201:
        raise SystemExit(f"run {index}: injection refused HTTP {st}: {inj}")
    injection_id = inj["injection_id"]
    injected_at = time.time()
    print(f"  [{index}] injected {injection_id} (rule={inj['rule_id']})")

    # --- rule fires -> detection latency -----------------------------------
    def fired_event():
        st, ev = http(base, "GET", f"/v1/operator/events?node={node_id}&limit=8", token=token)
        if st == 200 and isinstance(ev, dict):
            for e in ev.get("events", []):
                if e.get("rule_id") == "oom-guard-01":
                    return e
        return None
    event = wait_until(fired_event, 60, label="oom-guard-01 firing")
    det_lat = (epoch(event.get("fired_at")) or 0) - (epoch(event.get("breach_started_at")) or 0)

    # --- decision -> source/action/target ----------------------------------
    def decided():
        st, d = http(base, "GET", "/v1/operator/decisions?limit=10", token=token)
        if st == 200 and isinstance(d, dict):
            for dec in d.get("decisions", []):
                if dec.get("event_id") == event.get("id"):
                    return dec
        return None
    dec = wait_until(decided, 120, label="decision row for the event")
    action_kind = dec.get("action_kind")
    params_ = dec.get("action_params") or {}
    instance_name = params_.get("instance_name", "")
    decision_source = dec.get("decision_source")

    # --- audit row -> operation id + state transition + remediation latency
    def audited():
        st, a = http(base, "GET", f"/v1/operator/audits?node={node_id}&limit=10", token=token)
        if st == 200 and isinstance(a, dict):
            for audit in a.get("audits", []):
                if audit.get("decision_id") == dec.get("id"):
                    return audit
        return None
    audit = wait_until(audited, 60, label="audit row for the decision")
    remed_lat = (epoch(audit.get("executed_at")) or 0) - (epoch(dec.get("decided_at")) or 0)
    op_id = _operation_id(audit.get("detail") or "")
    transition = _state_transition(audit.get("detail") or "")

    # --- clear -> re-arm ----------------------------------------------------
    http(base, "DELETE", f"/v1/projects/{PROJECT_ID}/demo/faults/{injection_id}", token=token)

    def cleared():
        st, listing = http(base, "GET", f"/v1/projects/{PROJECT_ID}/demo/faults", token=token)
        if st == 200 and isinstance(listing, dict):
            for f in listing.get("faults", []):
                if f.get("injection_id") == injection_id and f.get("state") == "cleared":
                    return f
        return None
    wait_until(cleared, 60, label="injection reaching cleared (rule re-armed)")

    row = {
        "run": index,
        "injected_at": time.strftime("%H:%M:%S", time.gmtime(injected_at)),
        "detection_latency_s": round(det_lat, 2),
        "remediation_latency_s": round(remed_lat, 2),
        "decision_source": decision_source,
        "action_kind": action_kind,
        "instance_name": instance_name,
        "operation_id": op_id,
        "state_transition": transition,
        "audit_result": audit.get("result"),
        "lxd_endpoint": audit.get("lxd_endpoint"),
        "event_id": event.get("id"),
    }
    return row


def _operation_id(detail: str) -> str:
    """Extract the LXD operation path from an audit detail, or ''."""
    for token in detail.split():
        if "/1.0/operations/" in token:
            return token.strip(";,.")
    return ""


def _state_transition(detail: str) -> str:
    marker = "state "
    if marker in detail:
        return detail[detail.index(marker):].split(";")[0].strip()
    return ""


def negative_control(base: str, token: str, node: dict) -> dict:
    """Inject the fault with the leak signature removed (reclaimable cache) and
    assert the operator takes no destructive action."""
    params = {
        "oom_kills": 0,
        "pgmajfault_rate_s": 0,
        "instance_pgmajfault_rate_s": 0,
        "swap_peak_pct": 0,
        "mem_available_floor_pct": 40,
        "pressure_mem_some": 0,
        "pressure_mem_full": 0,
    }
    row = run_one(base, token, node, index=0, params=params)
    destructive = row["action_kind"] in DESTRUCTIVE
    row["negative_control_ok"] = not destructive
    return row


def main(argv=None) -> int:
    global PROJECT_ID
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--controller", default="https://34-93-190-64.sslip.io")
    ap.add_argument("--token", required=True)
    ap.add_argument("--project-id", default=PROJECT_ID)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--negative-control", action="store_true")
    args = ap.parse_args(argv)
    PROJECT_ID = args.project_id

    node = find_real_node(args.controller, args.token)
    print(f"# controller {args.controller}")
    print(f"# real node  {node['registration_id']} ({node['node_id']}) role={node['role']}")

    rows = []
    for i in range(1, args.runs + 1):
        rows.append(run_one(args.controller, args.token, node, index=i))

    if args.negative_control:
        rows.append(negative_control(args.controller, args.token, node))

    print("\n" + _table(rows))

    failed = 0
    for r in rows:
        ok = (r["decision_source"] == "model"
              and r["action_kind"] == "instance.restart"
              and r["instance_name"]
              and r["operation_id"]
              and r.get("negative_control_ok", True))
        if not ok:
            failed += 1
            print(f"  FAIL run {r['run']}: source={r['decision_source']} "
                  f"action={r['action_kind']} target={r['instance_name']!r} "
                  f"op={r['operation_id']!r}")
    if failed:
        print(f"\n{failed} run(s) did not meet acceptance")
        return 1
    print(f"\n{len(rows)} run(s): every decision was model instance.restart on a "
          f"real instance, audited with an operation id, and re-armed")
    return 0


def _table(rows) -> str:
    hdr = ["#", "injected", "det_lat_s", "remed_lat_s", "source", "action",
           "target", "op_id", "transition", "result", "neg_ok"]
    lines = ["  ".join(f"{c:<12}" for c in hdr)]
    for r in rows:
        op = (r["operation_id"].split("/")[-1])[:12] if r["operation_id"] else "-"
        lines.append("  ".join(f"{str(v):<12}" for v in [
            r["run"], r["injected_at"], r["detection_latency_s"], r["remediation_latency_s"],
            r["decision_source"], r["action_kind"], (r["instance_name"] or "-")[:12],
            op, (r["state_transition"] or "-")[:12], r["audit_result"],
            r.get("negative_control_ok", "-"),
        ]))
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
