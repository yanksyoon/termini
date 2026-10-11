"""Standalone operator CLI — runs the remediation loop as its own process.

This is the interface the chaos harness drives (see chaos/README.md):

    make chaos SCENARIO=oom OPERATOR_CMD="python3 controller/app/operator/cli.py \
        --controller-url {controller_url} --lxd-url {lxd_url} \
        --node {node_id} --project-token {project_token} \
        --thresholds {thresholds_json}"

Run directly:

    python3 -m app.operator.cli --controller-url http://127.0.0.1:8080 \
        --lxd-url http://127.0.0.1:8443 --node <node_id> --project-token <tok>

It polls the controller's telemetry store over HTTP (GET /v1/telemetry/recent),
evaluates the frozen thresholds through the same stateful `TriggerEngine` the
in-process loop uses (hysteresis + debounce), selects a playbook action, and
issues a real LXD REST command against --lxd-url. Before every poll it acquires
the controller's single-owner lease (POST /v1/operator/lease, migration 0007), so
it is a standby that issues nothing whenever the in-process loop (or another
standalone operator) already owns the project — remediation is idempotent across
operator instances. Trigger latch state persists to
--state-file (JSON) so a cold restart of the operator process re-arms the exact
same latch — this is what lets the dry-run exercise "persisted triggers" from a
cold process. Each remediation is audited to --audit-out (JSONL) with detection
and remediation latency recorded. --dry-run logs the intended command without
sending it.
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
from app.operator import detect, executor, playbook, triggers  # noqa: E402


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


def _load_state(state_file: str | None) -> dict:
    if not state_file:
        return {}
    p = pathlib.Path(state_file)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state_file: str | None, state: dict) -> None:
    if not state_file:
        return
    p = pathlib.Path(state_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True))
    tmp.replace(p)


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
    state_file: str | None = None,
    log=print,
) -> dict:
    recent_url = f"{controller_url.rstrip('/')}/v1/telemetry/recent?node={node_id}&limit={limit}"
    lease_url = f"{controller_url.rstrip('/')}/v1/operator/lease"
    lxd = executor.LxdExecutor(lxd_url, dry_run=dry_run, retry_max=2, backoff_s=0.5)
    engine = triggers.TriggerEngine(thresholds)
    state = _load_state(state_file)
    owner_id = "cli:" + ulid.new_ulid()
    fired: set[str] = set()
    audits: list[dict] = []
    started = time.monotonic()
    polls = 0
    standby = False

    while time.monotonic() - started < duration_s:
        polls += 1
        # Single-owner lease (migration 0007): only one operator may remediate per
        # project. Acquire/renew the lease through the controller; a non-owner is
        # a standby and issues nothing this poll. Fail closed on any error.
        ls_status, ls_body = _http_json(
            "POST", lease_url, body={"owner_id": owner_id},
            token=project_token, timeout=10,
        )
        owned = (ls_status == 200 and isinstance(ls_body, dict)
                 and ls_body.get("owner") is True)
        if not owned:
            if not standby:
                # The lease response's `owner_id` is the *current* holder, i.e.
                # the operator that actually owns the standby decision — not this
                # CLI's own `owner_id`, which is what leaked into the message before.
                lease_owner = (
                    ls_body.get("owner_id") if isinstance(ls_body, dict) else None
                )
                log(f"[operator] standby (lease held by {lease_owner}) "
                    f"self_id={owner_id}")
                standby = True
            time.sleep(poll_interval_s)
            continue
        standby = False

        status, body = _http_json("GET", recent_url, token=project_token, timeout=10)
        if status != 200 or not isinstance(body, dict):
            log(f"[operator] recent -> HTTP {status} {body}")
            time.sleep(poll_interval_s)
            continue
        samples = [s for s in body.get("samples", []) if isinstance(s, dict)]
        samples.reverse()  # controller returns DESC; evaluate ascending

        now = triggers.now_iso_ms()
        events, state = engine.evaluate(node_id, samples, state, now)
        _save_state(state_file, state)

        for event in events:
            fired.add(event.rule_id)
            detect_lat = triggers.detection_latency_s(event)
            action = playbook.choose(event, playbooks, instance_names, default_instance_name)
            if action is None:
                log(f"[operator] {event.rule_id} fired but no playbook action")
                continue
            t_fire = time.monotonic()
            result = lxd.execute(action)
            remed_lat = round(time.monotonic() - t_fire, 3)
            entry = {
                "ts": now,
                "node_id": node_id,
                "event_id": event.event_id,
                "rule": event.rule_id,
                "type": event.type,
                "metric": event.metric,
                "value": event.value,
                "threshold": event.threshold,
                "breach_started_at": event.breach_started_at,
                "fired_at": event.fired_at,
                "episode": event.episode,
                "detection_latency_s": detect_lat,
                "remediation_latency_s": remed_lat,
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
            log(f"[operator] FIRED {event.rule_id} (episode {event.episode}) -> {action.kind} "
                f"({result.method} {lxd_url.rstrip('/')}{result.path}) result={result.result} "
                f"detect={detect_lat}s remediate={remed_lat}s")
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
    ap.add_argument("--state-file", default=None,
                    help="persist trigger latch state (JSON) across restarts")
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
        state_file=args.state_file,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
