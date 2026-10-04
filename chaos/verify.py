#!/usr/bin/env python3
"""Chaos-harness acceptance suite — `make chaos-verify`.

Runs every scenario through an isolated stack (controller + node_sim + LXD stub
+ operator) and asserts the card's acceptance criteria:

    oom            sustained OOM series drives a real LXD remediation command
    disk-degrade   sustained disk-degradation series drives the disk playbook
    latency        latency series drives the latency rule
    healthy        no rule fires and no command is issued (false-positive guard)

Exits non-zero if any scenario fails. Safe to run in CI (no third-party deps;
ephemeral ports + temp DB, so it never touches a running controller).
"""
from __future__ import annotations

import argparse
import pathlib
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    __package__ = "chaos"

from chaos import config, run  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the full chaos acceptance suite")
    ap.add_argument("--config", default=None)
    ap.add_argument("--scenarios", default="oom,disk-degrade,latency,healthy")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cfg = config.load(args.config)
    names = [s.strip() for s in args.scenarios.split(",") if s.strip()]

    results = []
    for name in names:
        print(f"\n=== scenario: {name} ===")
        res = run.run_scenario(name, cfg)
        run._print_report(res)
        results.append(res)

    print("\n================ SUMMARY ================")
    failed = 0
    for res in results:
        status = "PASS" if res["passed"] else "FAIL"
        if not res["passed"]:
            failed += 1
        checks = res.get("checks", {})
        cmd = checks.get("remediation_command_issued", {})
        print(f"  {status}  {res['scenario']:14s} "
              f"injection={checks.get('injection_crosses_rule', {}).get('ok')} "
              f"commands={cmd.get('count')} (expected {'>=1' if cmd.get('expected') else '0'})")
    print(f"\n{len(results) - failed} passed, {failed} failed")

    if args.json:
        import json

        print(json.dumps(results, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
