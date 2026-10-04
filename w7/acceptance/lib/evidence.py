"""Standardized evidence recording.

Every check writes one timestamped artifact containing:
  * the exact command/request made,
  * the raw (unmodified) output,
  * the environment hash (controller URL, commit, node names, image tags).

The invariant: a result must be reproducible from the artifact alone.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class EvidenceRecorder:
    """Writes evidence files under a per-run directory."""

    def __init__(self, run_id: str, evidence_root: str, env_hash: dict):
        self.run_id = run_id
        self.root = os.path.abspath(evidence_root)
        self.run_dir = os.path.join(self.root, self.run_id)
        self.env_hash = env_hash
        os.makedirs(self.run_dir, exist_ok=True)

    def record(
        self,
        check_id: str,
        title: str,
        command: str,
        raw_output: str,
        *,
        notes: Optional[str] = None,
    ) -> str:
        """Write an evidence artifact and return its path (absolute)."""
        artifact = {
            "check_id": check_id,
            "title": title,
            "timestamp_utc": utcnow(),
            "command": command,
            "raw_output": raw_output,
            "env": self.env_hash,
        }
        if notes:
            artifact["notes"] = notes

        safe_id = check_id.replace(".", "_").replace("/", "_")
        # include a monotonic counter so multiple checks don't collide
        n = len(os.listdir(self.run_dir)) + 1
        fname = f"{n:02d}_{safe_id}.json"
        path = os.path.join(self.run_dir, fname)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(artifact, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        return path

    def write_summary(self, summary: dict, output_path: str) -> str:
        """Write the machine-readable summary JSON."""
        path = os.path.abspath(output_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        return path
