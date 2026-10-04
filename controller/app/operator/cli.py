"""Standalone operator CLI — runs the remediation loop as its own process.

This is the interface the chaos harness drives (see chaos/README.md):

    make chaos SCENARIO=oom OPERATOR_CMD="python3 controller/app/operator/cli.py \
        --controller-url {controller_url} --lxd-url {lxd_url} \
        --node {node_id} --project-token {project_token} \
        --thresholds {thresholds_json}"

Run directly:

    python3 -m app.operator.cli --controller-url http://127.0.0.1:8080 \
        --lxd-url http://127.0.0.1:8443 --node <node_id> --project-token <tok>

It polls the controller's telemetry store over HTTP (GET /v1/telemetry/recent,
the endpoint named in the task), evaluates the frozen thresholds, selects a
playbook action, and issues a real LXD REST command against --lxd-url. Each
remediation is audited to --audit-out (JSONL, one entry per fired rule) and is
fired at most once per rule per run (idempotency guard). --dry-run logs the
intended command without sending it.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

_CONTROLLER_DIR = pathlib.Path(__file__).resolve().parent.parent.parent
if str(_CONTROLLER_DIR) not in sys.path:
    sys.path.insert(0, str(_CONTROLLER_DIR))

from app import ulid  # noqa: E402
from app.operator import detect, executor, playbook  # noqa: E402


def _http_json(method: str, url: str, body=None, token: str | None = None,
               timeout: float = 10.0):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, (json.loads(raw) if raw else None)
        except json.JSONDecodeError:
            return e.code, raw.decode(errors="replace")
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, f"{type(e).__name__}: {e}"


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_operator(
    *,
    controller_url: str,
    project_token: str,
    node_id: str,
    lxd_url: str,
    thresholds: detect.Thresholds,
    playbooks: dict,
    instance_names: dict,
    default_instance_name: str,
    poll_interval_s: float,
    duration_s: float,
    dry_run: bool,
    audit_out: str | None,
    limit: int,
    log=print,
) -> dict:
    recent_url = f"{controller_url.rstrip('/')}/v1/telemetry/recent?node={node_id}&limit={limit}"
    lxd = executor.LxdExecutor(lxd_url, dry_run=dry_run, retry_max=2, backoff_s=0.5)
    fired: set[str] = set()
    audits: list[dict] = []
    started = time.monotonic()
    polls = 0

    while time.monotonic() - started < duration_s:
        polls += 1
        status, body = _http_json("GET", recent_url, token=project_token, timeout=10)
        if status != 200 or not isinstance(body, dict):
            log(f"[operator] recent -> HTTP {status} {body}")
            time.sleep(poll_interval_s)
            continue
        samples = [s for s in body.get("samples", []) if isinstance(s, dict)]
        samples.reverse()  # controller returns DESC; evaluate ascending

        events = detect.evaluate(node_id, samples, thresholds, _now_iso())
        for event in events:
            if event.rule_id in fired:
                continue
            action = playbook.choose(event, playbooks, instance_names, default_instance_name)
            if action is None:
                log(f"[operator] {event.rule_id} fired but no playbook action")
                continue
            result = lxd.execute(action)
            fired.add(event.rule_id)
            entry = {
                "ts": _now_iso(),
                "node_id": node_id,
                "event_id": event.event_id,
                "rule": event.rule_id,
                "type": event.type,
                "metric": event.metric,
                "value": event.value,
                "threshold": event.threshold,
                "action": action.kind,
                "action_params": action.params,
                "reason": action.reason,
                "dry_run": dry_run,
                "result": result.result,
                "lxd_method": result.method,
                "lxd_path": result.path,
                "lxd_body": result.body,
                "status": result.status,
                "detail": result.detail,
            }
            audits.append(entry)
            if audit_out:
                with open(audit_out, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry) + "\n")
            log(f"[operator] FIRED {event.rule_id} -> {action.kind} "
                f"({result.method} {lxd_url.rstrip('/')}{result.path}) result={result.result}")
        time.sleep(poll_interval_s)

    return {"polls": polls, "fired": sorted(fired), "audits": audits}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Termini AI-operator remediation loop (standalone)")
    ap.add_argument("--controller-url", required=True)
    ap.add_argument("--project-token", required=True)
    ap.add_argument("--node", required=True, help="target node_id")
    ap.add_argument("--lxd-url", required=True)
    ap.add_argument("--thresholds", default="{}",
                    help="thresholds JSON (chaos/chaos.json shape)")
    ap.add_argument("--playbooks-dir", default=None,
                    help="playbook directory (default: controller/playbooks)")
    ap.add_argument("--instance-names", default="{}",
                    help="JSON object mapping vm_id -> LXD instance name")
    ap.add_argument("--default-instance-name", default="agent")
    ap.add_argument("--poll-interval", type=float, default=1.0)
    ap.add_argument("--duration", type=float, default=20.0)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--audit-out", default=None, help="append audit records (JSONL) here")
    args = ap.parse_args(argv)

    try:
        thresholds = detect.Thresholds.from_dict(json.loads(args.thresholds))
    except json.JSONDecodeError as exc:
        print(f"--thresholds is not valid JSON: {exc}", file=sys.stderr)
        return 2

    try:
        instance_names = json.loads(args.instance_names)
        if not isinstance(instance_names, dict):
            raise ValueError("must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"--instance-names is not a valid JSON object: {exc}", file=sys.stderr)
        return 2

    playbooks_dir = args.playbooks_dir or str(_CONTROLLER_DIR / "playbooks")
    playbooks = playbook.load_playbooks(playbooks_dir)
    if not playbooks:
        print(f"[operator] warning: no playbooks loaded from {playbooks_dir}", file=sys.stderr)

    run_operator(
        controller_url=args.controller_url,
        project_token=args.project_token,
        node_id=args.node,
        lxd_url=args.lxd_url,
        thresholds=thresholds,
        playbooks=playbooks,
        instance_names={str(k): str(v) for k, v in instance_names.items()},
        default_instance_name=args.default_instance_name,
        poll_interval_s=args.poll_interval,
        duration_s=args.duration,
        dry_run=args.dry_run,
        audit_out=args.audit_out,
        limit=args.limit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
