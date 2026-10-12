#!/usr/bin/env python3
"""Termini W7 acceptance harness — single entrypoint.

Runs one independent check per W1–W6 acceptance criterion against the live demo
environment and emits a machine-readable summary JSON with one row per
criterion (pass/fail/skipped + evidence artifact path or explicit reason).

Configuration (env vars):
    TERMINI_CONTROLLER_URL    controller base URL      (default http://127.0.0.1:8085)
    TERMINI_PROJECT_TOKEN     frontend/project token   (default "")
    TERMINI_PROJECT_ID        demo project id          (default "")
    TERMINI_BOOTSTRAP_TOKEN   single-use enroll token  (default ""; enables register checks)
    TERMINI_DEMO_DB           controller SQLite path   (default /var/lib/termini/demo/controller.db)
    TERMINI_EVIDENCE_DIR      evidence output dir      (default <harness>/evidence)
    TERMINI_OUTPUT            summary JSON path        (default <harness>/summary.json)

Usage:
    TERMINI_PROJECT_TOKEN=... TERMINI_PROJECT_ID=... \
    TERMINI_BOOTSTRAP_TOKEN=... python3 run.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)

from lib import envhash  # noqa: E402
from lib.evidence import EvidenceRecorder, utcnow  # noqa: E402
from checks import REGISTRY, CheckContext  # noqa: E402


def _rel(repo_root: str, path: str) -> str:
    try:
        return os.path.relpath(path, repo_root)
    except ValueError:
        return path


def main() -> int:
    controller_url = os.environ.get("TERMINI_CONTROLLER_URL", "http://127.0.0.1:8085")
    project_token = os.environ.get("TERMINI_PROJECT_TOKEN", "")
    project_id = os.environ.get("TERMINI_PROJECT_ID", "")
    bootstrap_token = os.environ.get("TERMINI_BOOTSTRAP_TOKEN", "")
    evidence_dir = os.environ.get("TERMINI_EVIDENCE_DIR", os.path.join(HERE, "evidence"))
    output_path = os.environ.get("TERMINI_OUTPUT", os.path.join(HERE, "summary.json"))
    os.environ.setdefault("TERMINI_DEMO_DB", "/var/lib/termini/demo/controller.db")

    run_id = "run-" + utcnow().replace(":", "").replace(".", "").replace("T", "").replace("Z", "")

    print(f"== Termini W7 acceptance harness ==")
    print(f"controller_url : {controller_url}")
    print(f"project_id     : {project_id or '(unset)'}")
    print(f"project_token  : {'set' if project_token else '(unset)'}")
    print(f"bootstrap_token: {'set' if bootstrap_token else '(unset)'}")
    print(f"evidence_dir   : {evidence_dir}")
    print(f"output         : {output_path}")
    print()

    env = envhash.collect_env(controller_url, project_id, project_token, REPO_ROOT)
    ev = EvidenceRecorder(run_id, evidence_dir, env)
    ctx = CheckContext(
        controller_url=controller_url,
        project_token=project_token or None,
        project_id=project_id or None,
        bootstrap_token=bootstrap_token or None,
        repo_root=REPO_ROOT,
        evidence=ev,
        env_hash=env,
    )

    results = []
    for fn in REGISTRY:
        try:
            row = fn(ctx)
        except Exception as exc:  # noqa: BLE001
            # An exception is NOT a benign skip. Reporting it as 'skipped' made a
            # crashed check indistinguishable from an inapplicable one, and because
            # the exit code only counted 'fail', a stack broken enough that every
            # check crashed exited 0 (observed: W2.3 and W5.3 raised TimeoutError and
            # were silently downgraded). Crashes are recorded as 'error' and fail
            # closed in the exit code below.
            row = {
                "id": getattr(fn, "__name__", "?"),
                "workstream": "?",
                "criterion": "?",
                "test_method": "?",
                "status": "error",
                "detail": f"check raised {type(exc).__name__}: {exc}",
                "artifact": None,
                "reason": f"check crashed: {type(exc).__name__}: {exc}",
            }
        # normalize artifact to repo-relative path
        if row.get("artifact"):
            row["artifact"] = _rel(REPO_ROOT, row["artifact"])
        results.append(row)
        print(f"  [{row['status']:7}] {row['id']}  {row['detail'][:90]}")

    counts = {"pass": 0, "fail": 0, "skipped": 0, "error": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    summary = {
        "run_id": run_id,
        "generated_at_utc": utcnow(),
        "harness_commit": env.get("repo_commit"),
        "environment": env,
        "results": results,
        "totals": {**counts, "total": len(results)},
    }
    out = ev.write_summary(summary, output_path)
    print()
    print(f"totals: {counts}")
    print(f"summary written to {out}")

    # Exit non-zero if any criterion hard-fails OR any check errored, so CI can gate
    # on it. Failing closed on 'error' matters: a harness that reports a clean run
    # when it could not observe the system is worse than one that reports nothing.
    return 0 if (counts["fail"] == 0 and counts["error"] == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
