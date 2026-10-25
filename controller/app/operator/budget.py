"""Hourly model-call budget — the spend guard on the operator's diagnosis path.

WHY THIS EXISTS
---------------
`docs/contracts/operator-decision.md` §10.2 caps model calls only after a
*fallback* streak: a fault the model keeps answering SUCCESSFULLY is uncapped, so
the only rate limit on diagnosis spend is the per-``(node_id, rule_id)`` cooldown
(§10.2's sibling, `TERMINI_OPERATOR_COOLDOWN_S`). One configuration value being
wrong is therefore enough to turn a stuck trigger into one full-context model
call per poll, forever.

That is not hypothetical. On the standing fleet a leftover evidence drop-in
(`/etc/systemd/system/termini-controller.service.d/zz-evidence-cooldown.conf`)
pinned ``TERMINI_OPERATOR_COOLDOWN_S=0`` while the loop polled every 2 s in
``model`` mode. Cloud Monitoring measured the result: 42.1M input tokens on
2026-10-18 and 29.5M on 2026-10-19 (3,846 ``GenerateContent`` calls over the two
days, 12 of them HTTP 429 — the loop outran Vertex quota), ~$28 for a workload
whose useful decisions numbered in the tens.

This module is the guard that does not depend on a config value being right: a
sliding one-hour window over the calls the loop actually ATTEMPTS. Exceeding it
never loses a remediation — the loop takes the deterministic fast-path exactly as
§10.2 already does for a fallback streak, and says so in ONE warn line per
exhausted window.

WHAT IT COUNTS
--------------
Attempts, not successes. A call that times out or is rejected on the wire may
still be billed by the provider, so `record()` is called before the transport, not
after it.

THE TRADE THIS MAKES
--------------------
The window is in-process: a controller restart begins a fresh window. That is
deliberate — a restart also re-reads the configuration, and persisting the counter
would need a schema migration for a bound that is a safety net rather than an
accounting ledger (the authoritative spend record is the provider's own usage
metrics, not this counter). `operator-decision.md` §10.2 states the limitation so
a verifier does not have to discover it.

The budget is GLOBAL to one operator loop (all nodes, all rules), because the
thing it bounds — the cloud bill — is global. A per-``(node, rule)`` bound is what
the cooldown already does.
"""
from __future__ import annotations

import collections
import threading
import time

DEFAULT_WINDOW_S = 3600.0


class HourlyCallBudget:
    """A sliding-window cap on model-call attempts (thread-safe).

    ``limit_per_hour <= 0`` means unlimited: the guard is off and
    :meth:`allows` always answers True. That keeps the pre-existing behaviour
    available (a test harness, or an operator who has deliberately accepted
    unbounded spend) without a second code path.
    """

    def __init__(self, limit_per_hour: int, window_s: float = DEFAULT_WINDOW_S,
                 clock=time.monotonic) -> None:
        self.limit = max(0, int(limit_per_hour or 0))
        self.window_s = max(1.0, float(window_s or DEFAULT_WINDOW_S))
        self._clock = clock
        self._calls: collections.deque[float] = collections.deque()
        self._lock = threading.Lock()

    # -- internals --------------------------------------------------------- #
    def _prune(self, now: float) -> None:
        horizon = now - self.window_s
        while self._calls and self._calls[0] <= horizon:
            self._calls.popleft()

    # -- public ------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def used(self) -> int:
        """Attempts inside the current window (prunes expired entries)."""
        with self._lock:
            self._prune(self._clock())
            return len(self._calls)

    def allows(self) -> bool:
        """True when one more attempt fits in the window. Records nothing."""
        if not self.enabled:
            return True
        with self._lock:
            self._prune(self._clock())
            return len(self._calls) < self.limit

    def record(self) -> int:
        """Count one attempt; returns the number used in the window after it."""
        if not self.enabled:
            return 0
        with self._lock:
            now = self._clock()
            self._prune(now)
            self._calls.append(now)
            return len(self._calls)

    def remaining(self) -> int:
        if not self.enabled:
            return -1
        with self._lock:
            self._prune(self._clock())
            return max(0, self.limit - len(self._calls))

    def resume_at(self) -> float | None:
        """Monotonic instant the window frees up, or None when it already has."""
        if not self.enabled:
            return None
        with self._lock:
            now = self._clock()
            self._prune(now)
            if len(self._calls) < self.limit:
                return None
            return self._calls[0] + self.window_s

    def describe(self) -> str:
        """One-line state for a log line or a health/status surface."""
        if not self.enabled:
            return "model-call budget: unlimited"
        return (
            f"model-call budget: {self.used()}/{self.limit} calls in the last "
            f"{int(self.window_s)}s"
        )


__all__ = ["DEFAULT_WINDOW_S", "HourlyCallBudget"]
