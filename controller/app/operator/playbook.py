"""Declarative playbook loading + policy selection.

Playbooks are YAML files under `playbooks/` (parsed by `operator/yaml.py`).
Each declares which health `type` it handles and what remediation action to
take. The chooser is pure: given a HealthEvent it returns a RemediationAction
or None — it never touches the network or the database.

Playbook shape (see playbooks/*.yaml):

    version: 1
    rule_id: oom-guard-01
    type: oom
    description: <one line>
    action:
      kind: instance.restart      # instance.restart | instance.stop |
                                  # instance.delete | image.prune
      target: heaviest            # optional: literal instance name, or
                                  # `heaviest` = pick from telemetry evidence
      params:
        force: true
        timeout_s: 30
    reason: <one line, recorded in the decision log>
"""
from __future__ import annotations

import os
import pathlib

from . import yaml
from .types import HealthEvent, RemediationAction


def load_playbooks(playbooks_dir: str) -> dict[str, dict]:
    """Load every *.yaml playbook, keyed by health `type`."""
    out: dict[str, dict] = {}
    d = pathlib.Path(playbooks_dir)
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.yaml")):
        doc = yaml.loads(path.read_text())
        if not isinstance(doc, dict):
            raise yaml.YamlError(f"{path}: playbook root must be a mapping")
        if "type" not in doc or "action" not in doc:
            raise yaml.YamlError(f"{path}: playbook must declare `type` and `action`")
        if doc["type"] in out:
            raise yaml.YamlError(f"duplicate playbook type {doc['type']!r} in {playbooks_dir}")
        out[doc["type"]] = doc
    return out


def _heaviest_instance(event: HealthEvent) -> str | None:
    """Return the vm_id of the running instance with the most used RAM across the
    evidence window (the 'heaviest workload')."""
    best_ram = -1.0
    best_id = None
    for sample in event.evidence:
        instances = sample.get("lxd_instances")
        if not isinstance(instances, list):
            continue
        for inst in instances:
            if not isinstance(inst, dict):
                continue
            ram = inst.get("ram_used_bytes")
            if not isinstance(ram, (int, float)) or isinstance(ram, bool):
                continue
            if ram > best_ram:
                best_ram = ram
                best_id = inst.get("vm_id")
    return best_id


def choose(
    event: HealthEvent,
    playbooks: dict[str, dict],
    instance_names: dict[str, str] | None = None,
    default_instance_name: str = "agent",
) -> RemediationAction | None:
    pb = playbooks.get(event.type)
    if not pb:
        return None

    action_spec = pb.get("action")
    if not isinstance(action_spec, dict):
        return None

    kind = action_spec.get("kind")
    if not isinstance(kind, str) or not kind:
        return None

    params = dict(action_spec.get("params") or {})
    target = action_spec.get("target")

    if target == "heaviest":
        vm_id = _heaviest_instance(event)
        names = instance_names or {}
        name = names.get(vm_id, vm_id) if vm_id else default_instance_name
        params["instance_name"] = name
        params["_vm_id"] = vm_id
    elif target:
        params["instance_name"] = target

    return RemediationAction(
        kind=kind,
        params=params,
        reason=str(pb.get("reason", "") or pb.get("description", "")),
        playbook=str(pb.get("rule_id", "") or pb.get("type", "")),
        rule_id=event.rule_id,
    )
