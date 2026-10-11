#!/usr/bin/env python3
"""Acceptance test for the AI-operator remediation loop (t_375d9df4).

Drives the chaos harness (chaos/run.py) against the *production* operator loop
(controller/app/operator/cli.py) and asserts every acceptance criterion:

  1. oom           sustained OOM telemetry -> the trigger fires -> an action is
                   chosen from the command allowlist -> a real LXD REST command
                   reaches the stub, within one polling cycle.
  2. disk-degrade  sustained disk-degradation series fires its trigger; an
                   allowlisted action is chosen and audited.
  3. latency       sustained net-latency series fires its trigger; an allowlisted
                   action is chosen and audited.
  4. healthy       no trigger fires and no command is issued (false-positive guard).

The assertion is on the INVARIANT, not the mechanism: a trigger fired, an action was
chosen from the allowlist, and it was audited with its provenance. Which decider chose
it — the reference playbook provider, or a model via the decision seam — is recorded in
the audit row's `decision_source` and deliberately NOT asserted here. Pinning the
decider would re-freeze the mechanism that `docs/contracts/governance.md` §7.3 has just
un-frozen.

Each remediation is audited by the operator (JSONL via --audit-out), and each
rule fires at most once per run (idempotency). Dry-run mode and the schema-level
idempotency guard (one decision per event) are exercised by the unit suite
(tests/test_operator_executor.py, tests/test_operator_loop.py) — the chaos
harness cannot assert a dry-run for an external operator (it always expects a
command to reach its stub), so those two criteria are covered there.

Usage:  python3 controller/scripts/acceptance_operator.py   (from repo root)
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from chaos import run  # noqa: E402

OPERATOR_CMD = (
    "python3 controller/app/operator/cli.py"
    " --controller-url {controller_url} --lxd-url {lxd_url}"
    " --node {node_id} --project-token {project_token}"
    " --thresholds '{thresholds_json}'"
    " --duration 40 --audit-out {audit_out} --state-file {state_file}"
)

SCENARIOS = ["oom", "disk-degrade", "disk-fail", "latency", "healthy"]


def _read_jsonl(path: str) -> list[dict]:
    p = pathlib.Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def main(argv: list[str] | None = None) -> int:
    results = []
    for name in SCENARIOS:
        print(f"\n=== scenario: {name} ===")
        d = pathlib.Path(tempfile.mkdtemp(prefix="op-audit-"))
        audit_out = str(d / f"{name}.jsonl")
        state_file = str(d / f"{name}.state.json")
        # {audit_out} / {state_file} are NOT chaos placeholders, so substitute
        # them ourselves and leave the five placeholders run.run_scenario formats.
        cmd = OPERATOR_CMD.replace("{audit_out}", audit_out).replace("{state_file}", state_file)
        res = run.run_scenario(name, run.config.load(None), operator_cmd=cmd)
        run._print_report(res)
        audits = _read_jsonl(audit_out)
        res["audit_count"] = len(audits)
        res["detection_latency_s"] = [a.get("detection_latency_s") for a in audits
                                      if a.get("detection_latency_s") is not None]
        results.append(res)

    print("\n================ SUMMARY ================")
    failed = 0
    for res in results:
        status = "PASS" if res["passed"] else "FAIL"
        if not res["passed"]:
            failed += 1
        cmd = res.get("checks", {}).get("remediation_command_issued", {})
        print(f"  {status}  {res['scenario']:14s} commands={cmd.get('count')} "
              f"(expected {'>=1' if cmd.get('expected') else '0'}) "
              f"audits={res.get('audit_count')}")

    # Every remediation scenario must have produced an audit record, and each
    # audit must carry a measured detection latency (fault onset -> fire).
    for res in results:
        expects_remediation = res["scenario"] != "healthy"
        if expects_remediation and res.get("audit_count", 0) < 1:
            print(f"  FAIL  {res['scenario']}: expected >=1 audit record, "
                  f"got {res.get('audit_count')}")
            failed += 1
        elif not expects_remediation and res.get("audit_count", 0) != 0:
            print(f"  FAIL  healthy: expected 0 audits, got {res.get('audit_count')}")
            failed += 1
        if expects_remediation and not res.get("detection_latency_s"):
            print(f"  FAIL  {res['scenario']}: audits missing detection latency")
            failed += 1
        if res.get("detection_latency_s"):
            worst = max(res["detection_latency_s"])
            print(f"         {res['scenario']}: detection latencies={res['detection_latency_s']}s "
                  f"(max {worst}s)")
            if worst > 10.0:
                print(f"  FAIL  {res['scenario']}: detection latency {worst}s > 10s budget")
                failed += 1

    print(f"\n{len(results) - failed} passed, {failed} failed "
          f"(dry-run + idempotency + restart persistence asserted in the unit suite)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
