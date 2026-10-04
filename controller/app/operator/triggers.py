"""Stateful trigger engine — hysteresis + debounce over the pure detector.

The detector (`detect.py`) decides whether a telemetry window *currently*
breaches a frozen rule and whether it has *recovered*. This module adds the
state machine that makes a trigger deterministic and non-flapping:

    armed --(breach_run >= debounce)--> fired   (emit one HealthEvent)
    fired --(recovered >= clear_consecutive)--> armed   (re-arm, allow a future fire)

Properties this buys us (task t_6219d569):

  * **Debounce** — a single noisy sample can never fire: the breach run must
    reach `*_consecutive` samples (the frozen "N consecutive samples" rule).
  * **Hysteresis** — after firing, the trigger latches until the metric returns
    to a *distinct, healthier* clear threshold for `*_clear_consecutive`
    samples. A recovering metric that oscillates inside the band never re-fires.
  * **Persistence** — the state dict returned here is what the loop (SQLite via
    `operator/store.py`) and the standalone CLI (`--state-file`) persist, so a
    controller restart reloads the latch and never re-fires an already
    remediated fault.

The engine is pure: `evaluate` takes the current state and returns the next
state plus the events that fired. Storage is the caller's concern.
"""
from __future__ import annotations

import datetime

from . import detect
from .models import HealthEvent


def default_state() -> dict:
    return {"state": "armed", "last_fired_at": None,
            "last_cleared_at": None, "fired_count": 0}


def _breach_need(th: detect.Thresholds) -> dict:
    return {
        "oom-guard-01": th.oom_consecutive,
        "disk-fail-01": th.disk_fail_consecutive,
        "disk-degrade-01": min(th.disk_free_consecutive, th.disk_latency_consecutive),
        "latency-01": th.latency_consecutive,
    }


class TriggerEngine:
    """Evaluates telemetry windows against hysteretic threshold rules, tracking
    per-(node, rule) latch state across calls."""

    def __init__(self, thresholds: detect.Thresholds):
        self.thresholds = thresholds

    def evaluate(self, node_id: str, samples_asc: list[dict], state: dict,
                 now_iso: str) -> tuple[list[HealthEvent], dict]:
        """Return (fired_events, new_state). `state` maps rule_id -> latch dict
        (see `default_state`); missing rules are treated as armed."""
        th = self.thresholds
        new_state: dict = {}
        events: list[HealthEvent] = []

        if not samples_asc:
            # Preserve existing state verbatim when there is nothing to evaluate.
            for r in detect.RULE_IDS:
                new_state[r] = dict(state.get(r) or default_state())
            return events, new_state

        runs = detect.breach_streak(samples_asc, th)
        rec = detect.recovered(samples_asc, th)
        need = _breach_need(th)

        for rule_id in detect.RULE_IDS:
            cur = dict(state.get(rule_id) or default_state())
            streak = runs[rule_id]

            if cur.get("state") == "fired":
                if rec[rule_id]:
                    cur["state"] = "armed"
                    cur["last_cleared_at"] = now_iso
                # else: stay latched — a recovering metric must not re-fire.
            elif len(streak) >= need[rule_id]:
                breach_started_at = streak[0].get("ts") if streak else None
                events.append(detect.build_event(
                    rule_id, node_id, streak, th, now_iso, breach_started_at,
                ))
                cur["state"] = "fired"
                cur["last_fired_at"] = now_iso
                cur["fired_count"] = int(cur.get("fired_count", 0)) + 1
                events[-1].episode = cur["fired_count"]

            new_state[rule_id] = cur

        return events, new_state


def parse_epoch_ms(ts: str | None) -> float | None:
    """Fractional Unix seconds from an RFC3339 timestamp (ms or second precision)."""
    if not ts:
        return None
    try:
        from ..validate import parse_rfc3339_epoch
        return parse_rfc3339_epoch(ts)
    except (ValueError, KeyError):
        return None


def detection_latency_s(event: HealthEvent, now_iso: str | None = None) -> float | None:
    """Seconds from the first breaching sample's timestamp to the trigger fire
    (or to `now_iso`). This is the fault-onset -> trigger-fired latency."""
    start = parse_epoch_ms(event.breach_started_at)
    end = parse_epoch_ms(now_iso or event.fired_at)
    if start is None or end is None:
        return None
    return round(end - start, 3)


def now_iso_ms() -> str:
    """Current UTC time as RFC3339 with millisecond precision."""
    dt = datetime.datetime.now(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# --------------------------------------------------------------------------- #
# Durable threshold config (one subset per rule, keyed by rule_id).
# --------------------------------------------------------------------------- #
RULE_CONFIG_KEYS: dict[str, list[str]] = {
    "oom-guard-01": ["oom_ram_pct", "oom_clear_pct", "oom_consecutive", "oom_clear_consecutive"],
    "disk-fail-01": ["disk_fail_consecutive", "disk_fail_clear_consecutive"],
    "disk-degrade-01": ["disk_free_pct", "disk_free_clear_pct", "disk_free_consecutive",
                        "disk_write_latency_ms", "disk_write_latency_clear_ms",
                        "disk_latency_consecutive", "disk_free_slope_bps", "disk_clear_consecutive"],
    "latency-01": ["latency_ms", "latency_clear_ms", "latency_consecutive", "latency_clear_consecutive"],
}


def split_config(th: detect.Thresholds) -> dict:
    """Split a Thresholds object into per-rule config subsets for durable storage."""
    d = th.to_dict()
    return {r: {k: d[k] for k in keys} for r, keys in RULE_CONFIG_KEYS.items()}


def merge_config(parts: dict) -> detect.Thresholds:
    """Merge per-rule config subsets back into a Thresholds object. Missing rules
    or keys fall back to the frozen defaults via `from_dict`."""
    flat: dict = {}
    for subset in parts.values():
        if isinstance(subset, dict):
            flat.update(subset)
    return detect.Thresholds.from_dict(flat)
