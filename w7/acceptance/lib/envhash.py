"""Environment hash — makes every result reproducible to a specific environment.

Captures: repo commit, controller URL + version header, node names/roles,
image tags, and (where available) the LXD container list. This is the
"image tag, node name, commit" evidence the task requires.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Optional

from .http import http


def git_commit(repo_root: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def lxc_list() -> list:
    """Return the LXD container list (name/state/ipv4) or [] when LXD is absent."""
    try:
        out = subprocess.run(
            ["lxc", "list", "--format", "json"],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode != 0:
            return []
        data = json.loads(out.stdout or "[]")
        result = []
        for c in data:
            ipv4 = ""
            state = c.get("state") or {}
            network = state.get("network") or {}
            eth0 = network.get("eth0") or {}
            for addr in eth0.get("addresses", []) or []:
                if addr.get("family") == "inet":
                    ipv4 = addr.get("address", "")
                    break
            result.append({
                "name": c.get("name"),
                "state": c.get("status"),
                "ipv4": ipv4,
                "type": c.get("type"),
            })
        return result
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []


def collect_env(controller_url: str, project_id: str, project_token: str, repo_root: str) -> dict:
    env: dict = {
        "controller_url": controller_url,
        "project_id": project_id,
        "repo_commit": git_commit(repo_root),
        "controller_version_header": None,
        "nodes": [],
        "lxd_containers": lxc_list(),
        "collected_at_utc": None,
    }
    from .evidence import utcnow
    env["collected_at_utc"] = utcnow()

    # Controller version + node list via the project-token surface.
    if project_token:
        try:
            r = http("GET", controller_url, f"/v1/projects/{project_id}/nodes",
                     token=project_token, timeout=10)
            env["controller_version_header"] = r.headers.get("X-Termini-Version")
            body = r.json() or {}
            env["nodes"] = [
                {
                    "node_id": n.get("node_id"),
                    "role": n.get("role"),
                    "status": n.get("status"),
                    "registration_id": n.get("registration_id"),
                    "image_tag": (n.get("hardware") or {}).get("image_version")
                    or n.get("image_version"),
                }
                for n in body.get("nodes", [])
            ]
        except Exception as exc:  # noqa: BLE001 - best effort
            env["nodes_error"] = f"{type(exc).__name__}: {exc}"

    return env
