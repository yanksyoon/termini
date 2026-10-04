"""AI-operator remediation loop: detect -> choose -> execute -> audit.

Submodules:
  yaml      minimal YAML-subset parser (stdlib-only, no PyYAML)
  types     HealthEvent / RemediationAction / ExecResult dataclasses
  detect    threshold detector (pure)
  playbook  playbook loading + action selection (pure)
  executor  LXD REST client (dry-run, retry/backoff, idempotent ops)
  store     health-event / decision / audit persistence
  loop      the orchestrating thread (poll -> detect -> choose -> execute -> audit)
"""
from .detect import Thresholds
from .executor import LxdExecutor
from .loop import OperatorLoop

__all__ = ["Thresholds", "LxdExecutor", "OperatorLoop"]
