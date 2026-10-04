"""`termini-agent` CLI (agent-runtime.md §5.1, §18)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any

from . import __version__

RUNTIME_IMPORTS = [
    "openai",
    "httpx",
    "pydantic",
    "yaml",
    "tenacity",
    "structlog",
    "langchain_core",
    "langgraph",
    "google.adk",
]

# Each isolated framework venv and the module that proves its headline package loads.
FRAMEWORK_CHECKS: dict[str, str] = {
    "langchain": "langchain",
    "crewai": "crewai",
    "autogen": "autogen_agentchat",
}


def _cmd_run(args: argparse.Namespace) -> int:
    from .runtime import run_from_config

    return run_from_config(args.config, status_unix=args.status_socket,
                           status_http_port=args.status_port)


def _cmd_status(args: argparse.Namespace) -> int:
    import httpx

    try:
        resp = httpx.get(f"http://127.0.0.1:{args.status_port}/status", timeout=2.0)
        resp.raise_for_status()
        print(json.dumps(resp.json(), indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


def _cmd_selfcheck(args: argparse.Namespace) -> int:
    report: dict[str, Any] = {
        "kind": "termini.agent_selfcheck",
        "agent_version": __version__,
        "python": sys.version.split()[0],
        "runtime_imports": {},
        "framework_venvs": {},
        "config": {},
        "ok": True,
    }

    for mod in RUNTIME_IMPORTS:
        try:
            __import__(mod)
            report["runtime_imports"][mod] = "ok"
        except Exception as exc:  # noqa: BLE001
            report["runtime_imports"][mod] = f"FAIL: {exc}"
            report["ok"] = False

    # isolated framework venvs: /opt/termini/venvs/<name>/bin/python
    fw_root = args.frameworks_dir
    for name, module in FRAMEWORK_CHECKS.items():
        py = os.path.join(fw_root, name, "bin", "python")
        if not os.path.exists(py):
            report["framework_venvs"][name] = "MISSING venv"
            report["ok"] = False
            continue
        proc = subprocess.run([py, "-c", f"import {module}"], capture_output=True, text=True)
        if proc.returncode == 0:
            report["framework_venvs"][name] = "ok"
        else:
            report["framework_venvs"][name] = f"FAIL: {proc.stderr.strip().splitlines()[-1] if proc.stderr else 'import error'}"
            report["ok"] = False

    # config (optional; validated against the on-prem allow-list)
    if args.config and os.path.exists(args.config):
        try:
            from .config import load_config

            cfg = load_config(args.config, dev_override=args.dev)
            report["config"] = {"path": args.config, "status": "valid",
                                "inference_base_url": cfg.inference.base_url,
                                "default_model": cfg.inference.default_model,
                                "allowed_models": list(cfg.inference.allowed_models)}
        except Exception as exc:  # noqa: BLE001
            report["config"] = {"path": args.config, "status": f"FAIL: {exc}"}
            report["ok"] = False
    else:
        report["config"] = {"path": args.config, "status": "skipped"}

    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="termini-agent",
                                description="Termini in-VM agent runtime")
    p.add_argument("--version", action="version", version=f"termini-agent {__version__}")
    sub = p.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the task loop")
    run.add_argument("--config", default="/etc/termini/agent.yaml")
    run.add_argument("--status-socket", default="/run/termini/agent.sock")
    run.add_argument("--status-port", type=int, default=9101)
    run.set_defaults(func=_cmd_run)

    st = sub.add_parser("status", help="print the local status snapshot")
    st.add_argument("--status-port", type=int, default=9101)
    st.set_defaults(func=_cmd_status)

    sc = sub.add_parser("selfcheck", help="verify baked deps + config import offline")
    sc.add_argument("--config", default="/etc/termini/agent.yaml")
    sc.add_argument("--frameworks-dir", default="/opt/termini/venvs")
    sc.add_argument("--dev", action="store_true", help="relax the inference allow-list (tests)")
    sc.set_defaults(func=_cmd_selfcheck)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
