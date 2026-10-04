"""AI-operator remediation loop: detect -> choose -> execute -> audit.

Submodules:
  yaml      minimal YAML-subset parser (stdlib-only, no PyYAML)
  models    HealthEvent / RemediationAction / ExecResult dataclasses
  detect    threshold detector (pure signal extraction + rule predicates)
  triggers  stateful trigger engine (hysteresis + debounce + persistence model)
  playbook  playbook loading + action selection (pure)
  executor  LXD REST client (dry-run, retry/backoff, idempotent ops)
  store     health-event / decision / audit / trigger-config / trigger-state persistence
  loop      the in-process orchestrating thread (poll -> detect -> choose -> execute -> audit)
  cli       standalone operator process (poll /v1/telemetry/recent, execute, audit JSONL)
"""
from .detect import Thresholds
from .executor import LxdExecutor
from .loop import OperatorLoop
from .triggers import TriggerEngine

__all__ = ["Thresholds", "LxdExecutor", "OperatorLoop", "TriggerEngine"]
