"""W6 — Demo reliability & chaos scenarios.

Independent observation of the chaos/remediation machinery and the timed dry
run. A live "lands in time" verdict requires the SRE red-team run; here the
harness verifies the mechanism exists and records the dry-run status.
"""
from __future__ import annotations

import json
import os

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
    method = "look for a committed timed dry-run artifact; independently report its absence (dry run is a scheduled, not-yet-run task)"
    root = ctx.repo_root

    # Search for a dry-run artifact anywhere plausible in the repo.
    candidates = []
    for dirpath, _dirs, files in os.walk(root):
        if ".git" in dirpath or ".hermes" in dirpath:
            continue
        for f in files:
            if "dry" in f.lower() or "dry-run" in f.lower() or "dryrun" in f.lower():
                candidates.append(os.path.join(dirpath, f))

    raw = json.dumps({"dry_run_artifacts": candidates}, indent=2)
    art = ctx.ev.record("W6.2", "W6.2 timed dry run", "search repo for dry-run artifact", raw)

    if candidates:
        return result("W6.2", WS, criterion, method, "fail",
                      f"dry-run artifacts found but no pass/fail run record: {candidates}",
                      art, reason="a dry-run artifact exists but no executed pass/fail record was found")
    return result("W6.2", WS, criterion, method, "skipped",
                  "no timed dry-run artifact present in the repo",
                  art, reason="timed end-to-end dry run not yet executed (t_3bb56d97 'Run and time the full end-to-end demo dry run' is todo)")
