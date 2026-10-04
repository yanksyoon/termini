"""Termini in-VM agent runtime.

Implements the in-VM side of docs/contracts/agent-runtime.md §5: a task loop that
claims work from the on-node broker, calls the on-prem OpenAI-compatible inference
router, checkpoints task state locally, and exposes a local-only status surface.

Nothing in this package egresses except:
  * the on-node broker (config `broker.endpoint`), and
  * the on-prem inference router (config `inference.base_url`).
Both are asserted against a hard allow-list at load time (config.py).
"""

__version__ = "2026.10.0"
