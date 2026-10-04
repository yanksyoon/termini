#!/usr/bin/env python3
"""Reference operator probe — closes the injection -> detection -> LXD chain.

STATUS: this is a *stand-in*, not the production remediation loop (that is
task t_375d9df4, `termini` operator loop). It exists so the chaos harness can
prove end-to-end that a simulated failure series actually results in a real LXD
REST call, before/independently of the production loop.

It implements exactly the four frozen demo rules from
docs/contracts/node-telemetry.md §5 (OOM, disk-degrade, latency) with no policy
of its own:

    1. poll the controller telemetry store (GET /v1/telemetry/recent)
    2. evaluate deterministic thresholds (chaos/common.evaluate_series)
    3. map the firing rule to one allowlisted LXD REST call
    4. record an audit entry; fire at most once per rule (idempotency guard)

Point the harness at the real loop instead with:

    make chaos SCENARIO=oom OPERATOR_CMD="python3 -m <real loop> ..."
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # allow `python3 chaos/reference_operator.py`

from chaos import common  # noqa: E402

# rule_id -> (action label, LXD method, path template, body)
ACTIONS = {
    "oom-guard-01": ("vm.restart", "POST", "/1.0/instances/{instance}/state", {"action": "restart", "force": True}),
    "disk-degrade-01": ("disk.drain", "POST", "/1.0/instances/{instance}/state", {"action": "stop", "force": True}),
    "latency-01": ("net.probe", "POST", "/1.0/instances/{instance}/state", {"action": "restart"}),
}


def select_action(samples: list[dict], thresholds: dict) -> dict | None:
    """Pure: given an ascending sample series, return the first firing rule.

    Deterministic precedence: OOM, then disk-degrade, then latency.
    """
    results = common.evaluate_series(samples, thresholds)
    for rule in ("oom-guard-01", "disk-degrade-01", "latency-01"):
        if results[rule]["fired"]:
            last = samples[-1] if samples else {}
            ram_pct = _ram_pct(last)
            return {
                "rule": rule,
                "run": results[rule]["run"],
                "needed": results[rule]["needed"],
                "action": ACTIONS[rule][0],
                "evidence": {
                    "ram_pct": round(ram_pct, 2) if ram_pct is not None else None,
                    "disk_write_latency_ms": _disk_lat(last),
                    "net_latency_ms": _net_lat(last),
                },
            }
    return None


def _ram_pct(s: dict) -> float | None:
    total, used = s.get("ram_total_bytes"), s.get("ram_used_bytes")
    if isinstance(total, (int, float)) and total and isinstance(used, (int, float)):
        return used / total * 100.0
    return None


def _disk_lat(s: dict):
    d = s.get("disk")
    return d[0].get("write_latency_ms") if isinstance(d, list) and d and isinstance(d[0], dict) else None


def _net_lat(s: dict):
    n = s.get("net")
    return n[0].get("latency_ms") if isinstance(n, list) and n and isinstance(n[0], dict) else None


def _instance_of(sample: dict, fallback: str) -> str:
    inst = sample.get("lxd_instances")
    if isinstance(inst, list) and inst and isinstance(inst[0], dict) and inst[0].get("vm_id"):
        return str(inst[0]["vm_id"])
    return fallback


def issue(decision: dict, sample: dict, lxd_url: str, dry_run: bool) -> dict:
    _, method, path_tmpl, body = ACTIONS[decision["rule"]]
    path = path_tmpl.format(instance=_instance_of(sample, "agent-vm-1"))
    url = lxd_url.rstrip("/") + path
    if dry_run:
        return {"status": "dry_run", "code": 0, "method": method, "url": url, "body": body}
    status, resp = common.http_json(method, url, body, timeout=10)
    return {"status": "sent", "code": status, "method": method, "url": url, "body": body, "response": resp}


def run_operator(
    *,
    controller_url: str,
    project_token: str,
    node_id: str,
    lxd_url: str,
    thresholds: dict,
    poll_interval_s: float,
    duration_s: float,
    dry_run: bool = False,
    audit_out: str | None = None,
    log=print,
) -> dict:
    recent_url = f"{controller_url.rstrip('/')}/v1/telemetry/recent?node={node_id}&limit=100"
    fired: set[str] = set()
    decisions: list[dict] = []
    started = time.monotonic()
    polls = 0

    while time.monotonic() - started < duration_s:
        polls += 1
        status, body = common.http_json("GET", recent_url, token=project_token, timeout=10)
        if status != 200 or not isinstance(body, dict):
            log(f"[operator] recent -> HTTP {status} {body}")
            time.sleep(poll_interval_s)
            continue
        samples = [s for s in body.get("samples", []) if isinstance(s, dict)]
        samples.reverse()  # controller returns DESC; evaluate ascending

        decision = select_action(samples, thresholds)
        if decision and decision["rule"] not in fired:
            sample = samples[-1]
            result = issue(decision, sample, lxd_url, dry_run)
            fired.add(decision["rule"])
            entry = {
                "ts": common.rfc3339_now(),
                "node_id": node_id,
                "rule": decision["rule"],
                "action": decision["action"],
                "run": decision["run"],
                "needed": decision["needed"],
                "evidence": decision["evidence"],
                "dry_run": dry_run,
                "result": result,
            }
            decisions.append(entry)
            if audit_out:
                common.append_jsonl(audit_out, entry)
            log(f"[operator] FIRED {decision['rule']} -> {decision['action']} "
                f"({result['method']} {result['url']}) code={result['code']}")
        time.sleep(poll_interval_s)

    return {"polls": polls, "fired": sorted(fired), "decisions": decisions}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Reference operator probe (frozen demo rules)")
    ap.add_argument("--controller-url", required=True)
    ap.add_argument("--project-token", required=True)
    ap.add_argument("--node", required=True, help="target node_id")
    ap.add_argument("--lxd-url", required=True)
    ap.add_argument("--thresholds", default="{}", help="thresholds JSON string")
    ap.add_argument("--poll-interval", type=float, default=1.0)
    ap.add_argument("--duration", type=float, default=20.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--audit-out", default=None)
    args = ap.parse_args(argv)

    import json

    try:
        thresholds = json.loads(args.thresholds)
    except json.JSONDecodeError as exc:
        print(f"--thresholds is not valid JSON: {exc}", file=sys.stderr)
        return 2

    run_operator(
        controller_url=args.controller_url,
        project_token=args.project_token,
        node_id=args.node,
        lxd_url=args.lxd_url,
        thresholds=thresholds,
        poll_interval_s=args.poll_interval,
        duration_s=args.duration,
        dry_run=args.dry_run,
        audit_out=args.audit_out,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
