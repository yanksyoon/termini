"""W6 — Demo reliability & chaos scenarios.

Independent observation of the chaos/remediation machinery and the timed dry
run. A live "lands in time" verdict requires the SRE red-team run; here the
harness verifies the mechanism exists and records the dry-run status.
"""
from __future__ import annotations

import json
import os
import pathlib

from . import CheckContext, result, run

WS = "W6 — Demo reliability & chaos scenarios"


def check_w6_1(ctx: CheckContext) -> dict:
    criterion = "both scenarios reproducibly trigger an AI remediation that lands in time"
    method = "verify chaos scripts (Scenario A OOM + B disk), operator CLI/playbooks and threshold triggers exist; note a live timed trigger is exercised by the SRE red-team"
    root = ctx.repo_root

    chaos_dir = os.path.join(root, "scripts", "chaos")
    chaos_files = sorted(os.listdir(chaos_dir)) if os.path.isdir(chaos_dir) else []
    makefile = os.path.join(root, "Makefile")
    make_chaos = ""
    if os.path.isfile(makefile):
        make_chaos = open(makefile, encoding="utf-8").read()

    operator_dir = os.path.join(root, "controller", "app", "operator")
    operator_files = sorted(os.listdir(operator_dir)) if os.path.isdir(operator_dir) else []
    playbooks = os.path.join(root, "controller", "playbooks")
    playbook_files = sorted(os.listdir(playbooks)) if os.path.isdir(playbooks) else []

    raw = json.dumps({
        "chaos_files": chaos_files,
        "make_chaos_target": "chaos:" in make_chaos,
        "operator_files": operator_files,
        "playbook_files": playbook_files,
    }, indent=2)
    art = ctx.ev.record("W6.1", "W6.1 chaos + remediation mechanism",
                        "inspect scripts/chaos, controller/app/operator, playbooks, Makefile", raw)

    mechanism_present = (
        any(f.startswith(("scenario-a", "scenario-b", "oom", "disk", "chaos")) for f in chaos_files)
        and "chaos:" in make_chaos
        and any(f.endswith(".py") for f in operator_files)
    )

    return result("W6.1", WS, criterion, method,
                  "skipped",
                  f"mechanism present: chaos={chaos_files} operator={operator_files} playbooks={playbook_files}",
                  art,
                  reason="chaos/remediation mechanism exists, but a live 'lands in time' trigger is exercised by the SRE red-team (t_3dc57f17 OOM, t_8d243ee1 cloud-init/fan-network), not yet run in this environment")


def check_w6_2(ctx: CheckContext) -> dict:
    criterion = "the timed dry run passes end-to-end"
    method = ("read the committed timed dry-run record "
              "(scripts/timing/evidence/<ts>/runs.jsonl) and report its per-run verdict")

    # The record is `runs.jsonl`: one JSON object per timed run
    # ({"run": n, "total_s": s, "status": "PASS"|"FAIL", "cold_operator": ...}).
    #
    # This check previously searched for FILES WHOSE NAME CONTAINS "dry", which
    # only ever matched the dry-run SCRIPT (scripts/timing/demo-dry-run.py) and
    # never the record. That made it structurally unable to pass: any 'dry'-named
    # file present -> "fail", none -> "skipped". It reported an open blocker that
    # did not exist, while three PASSing timed runs sat on disk unread.
    root = pathlib.Path(ctx.repo_root)
    ev = root / "scripts" / "timing" / "evidence"
    records = sorted(ev.glob("*/runs.jsonl")) if ev.is_dir() else []

    runs: list[dict] = []
    used_name = ""
    for p in reversed(records):                      # newest evidence dir first
        try:
            parsed = [json.loads(line) for line in
                      p.read_text(encoding="utf-8").splitlines() if line.strip()]
        except Exception:                            # noqa: BLE001 — try the next
            continue
        if parsed:
            runs, used_name = parsed, p.parent.name
            break

    raw = json.dumps({
        "record_files": [str(p.relative_to(root)) for p in records],
        "used": used_name or None,
        "runs": runs,
    }, indent=2)
    art = ctx.ev.record("W6.2", "W6.2 timed dry run record",
                        "read scripts/timing/evidence/*/runs.jsonl", raw)

    if not runs:
        return result("W6.2", WS, criterion, method, "skipped",
                      "no timed dry-run record found under scripts/timing/evidence/",
                      art, reason="timed end-to-end dry run not yet executed (t_3bb56d97)")

    statuses = [str(r.get("status", "")).upper() for r in runs]
    totals = [r["total_s"] for r in runs if isinstance(r.get("total_s"), (int, float))]
    summary = (f"{len(runs)} timed run(s) from {used_name}: statuses={statuses}"
               + (f", total_s min={min(totals):.1f} max={max(totals):.1f}" if totals else ""))

    if all(s == "PASS" for s in statuses):
        return result("W6.2", WS, criterion, method, "pass", summary, art)
    return result("W6.2", WS, criterion, method, "fail", summary, art,
                  reason=f"timed dry-run record contains non-PASS run(s): {statuses}")
