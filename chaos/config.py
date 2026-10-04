"""Chaos-harness configuration — `chaos/chaos.json` plus environment overrides.

The JSON file is the single source of truth for scenario parameters
(thresholds, cadence, duration). Every knob can be overridden from the
environment so a demo or a CI run can retune without editing the file:

    TERMINI_CHAOS_CONFIG            path to an alternate config JSON
    TERMINI_CHAOS_CONTROLLER_URL    http://host:port   (controller base URL)
    TERMINI_CHAOS_BOOTSTRAP_TOKEN   single-use registration token
    TERMINI_CHAOS_PROJECT_TOKEN     project/operator token
    TERMINI_CHAOS_LXD_URL           point at a real LXD REST endpoint (implies real mode)
    TERMINI_CHAOS_LXD_MODE          stub | real
    TERMINI_CHAOS_CADENCE_MS        telemetry cadence
    TERMINI_CHAOS_OPERATOR_CMD      command that runs the production operator loop
    TERMINI_CHAOS_DRY_RUN           truthy -> operator logs the command instead of sending
    TERMINI_CHAOS_TH_<NAME>         override a threshold, e.g. TERMINI_CHAOS_TH_OOM_RAM_PCT
    TERMINI_CHAOS_DURATION_<NAME>   override a scenario duration (name uppercased, - -> _)

Nothing here requires third-party packages.
"""
from __future__ import annotations

import os
import pathlib
from typing import Any, Mapping

from . import common

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_PATH = HERE / "chaos.json"

_TRUTHY = {"1", "true", "yes", "on"}


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in _TRUTHY


def _split_url(url: str) -> tuple[str, int]:
    from urllib.parse import urlsplit

    parts = urlsplit(url if "://" in url else "http://" + url)
    if not parts.hostname:
        raise ValueError(f"not a usable URL: {url!r}")
    return parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def apply_env(cfg: dict, env: Mapping[str, str] | None = None) -> dict:
    """Mutate `cfg` in place with environment overrides; returns it."""
    src: Mapping[str, str] = os.environ if env is None else env

    if src.get("TERMINI_CHAOS_CONTROLLER_URL"):
        cfg["controller"]["host"], cfg["controller"]["port"] = _split_url(
            src["TERMINI_CHAOS_CONTROLLER_URL"]
        )
    if src.get("TERMINI_CHAOS_BOOTSTRAP_TOKEN"):
        cfg["controller"]["bootstrap_token"] = src["TERMINI_CHAOS_BOOTSTRAP_TOKEN"]
    if src.get("TERMINI_CHAOS_PROJECT_TOKEN"):
        cfg["controller"]["project_token"] = src["TERMINI_CHAOS_PROJECT_TOKEN"]

    if src.get("TERMINI_CHAOS_LXD_URL"):
        cfg["lxd"]["url"] = src["TERMINI_CHAOS_LXD_URL"]
        cfg["lxd"]["mode"] = "real"
    if src.get("TERMINI_CHAOS_LXD_MODE"):
        cfg["lxd"]["mode"] = src["TERMINI_CHAOS_LXD_MODE"].strip().lower()

    if src.get("TERMINI_CHAOS_CADENCE_MS"):
        cfg["node"]["cadence_ms"] = int(src["TERMINI_CHAOS_CADENCE_MS"])
    if src.get("TERMINI_CHAOS_OPERATOR_CMD"):
        cfg["operator"]["cmd"] = src["TERMINI_CHAOS_OPERATOR_CMD"]
    if src.get("TERMINI_CHAOS_DRY_RUN"):
        cfg["operator"]["dry_run"] = _as_bool(src["TERMINI_CHAOS_DRY_RUN"])

    for key in list(cfg["thresholds"].keys()):
        raw = src.get("TERMINI_CHAOS_TH_" + key.upper())
        if raw not in (None, ""):
            cfg["thresholds"][key] = float(raw) if "." in raw else int(raw)

    for name, scen in cfg["scenarios"].items():
        suffix = name.upper().replace("-", "_")
        raw = src.get("TERMINI_CHAOS_DURATION_" + suffix)
        if raw not in (None, ""):
            scen["duration_s"] = int(raw)
        raw = src.get("TERMINI_CHAOS_PARAM_" + suffix)
        if raw not in (None, "") and pathlib.Path(raw).exists():
            scen["params"].update(common.read_json(raw))

    return cfg


def load(path: str | os.PathLike | None = None, env: Mapping[str, str] | None = None) -> dict:
    src: Mapping[str, str] = os.environ if env is None else env
    cfg_path = path or src.get("TERMINI_CHAOS_CONFIG") or DEFAULT_PATH
    cfg: dict[str, Any] = common.read_json(cfg_path)
    return apply_env(cfg, src)


def controller_url(cfg: dict) -> str:
    return f"http://{cfg['controller']['host']}:{cfg['controller']['port']}"


def lxd_url(cfg: dict) -> str:
    if cfg["lxd"].get("url"):
        return str(cfg["lxd"]["url"]).rstrip("/")
    return f"http://{cfg['lxd']['host']}:{cfg['lxd']['port']}"


def scenario_names(cfg: dict) -> list[str]:
    return list(cfg["scenarios"].keys())


def get_scenario(cfg: dict, name: str) -> dict:
    if name not in cfg["scenarios"]:
        raise KeyError(f"unknown scenario {name!r}; known: {', '.join(scenario_names(cfg))}")
    return cfg["scenarios"][name]
