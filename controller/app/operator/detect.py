"""Threshold detector — pure functions that turn a telemetry window into
structured HealthEvents. No I/O, no side effects: the trigger engine feeds it
samples read from the store and consumes the events it returns.

Detection follows docs/contracts/node-telemetry.md §5 (frozen demo rules) and the
chaos harness's evidence (`chaos/chaos.json`), which drive the same signals:

  oom-guard-01    ram_used_bytes/ram_total_bytes >= oom_ram_pct for
                  oom_consecutive consecutive samples               (memory pressure)
  disk-fail-01    any disk[].mount_ro == true                       (EXT4 remount-ro)
  disk-degrade-01 any of: free/total <= disk_free_pct for disk_free_consecutive,
                  OR disk[].write_latency_ms >= disk_write_latency_ms for
                  disk_latency_consecutive, OR free_bytes falling faster than
                  disk_free_slope_bps (steep negative slope)
  latency-01      net[].latency_ms >= latency_ms for latency_consecutive samples

Each rule is *hysteretic*: it has an **enter** threshold (fire) and a distinct
**clear** threshold (re-arm), plus a debounce count on each edge. The stateful
machinery (see `operator/triggers.py`) uses `breach_streak` to fire (currently
breached for N consecutive samples) and `recovered` to re-arm, so a single
noisy sample never fires and a recovering metric never re-fires within the
hysteresis band. Thresholds come from config/durable storage, never from code —
see `Thresholds`.

Note on p95: the frozen telemetry schema has no application p95 field — the only
latency signal is `net[].latency_ms` (see `chaos/README.md`). The latency rule
therefore thresholds that field.
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import ulid
from ..validate import parse_rfc3339_epoch
from .models import HealthEvent


@dataclass(frozen=True)
class Thresholds:
    # --- oom (memory pressure) ---------------------------------------------
    oom_ram_pct: float = 95.0           # enter: mem% >= this fires
    oom_clear_pct: float = 90.0         # clear: mem% <= this re-arms
    oom_consecutive: int = 2            # debounce: N consecutive breaching samples
    oom_clear_consecutive: int = 3      # hysteresis: N consecutive healthy samples
    # --- disk-fail (EXT4 remount read-only) --------------------------------
    disk_fail_consecutive: int = 1      # any mount_ro true (single sample fires)
    disk_fail_clear_consecutive: int = 3
    # --- disk-degrade ------------------------------------------------------
    disk_free_pct: float = 5.0          # enter: free% floor
    disk_free_clear_pct: float = 15.0   # clear: free% must recover above this
    disk_free_consecutive: int = 3
    disk_write_latency_ms: float = 50.0
    disk_write_latency_clear_ms: float = 30.0
    disk_latency_consecutive: int = 5
    disk_free_slope_bps: float = -5_000_000.0   # bytes/sec (negative => falling)
    disk_clear_consecutive: int = 3
    # --- latency -----------------------------------------------------------
    latency_ms: float = 250.0
    latency_clear_ms: float = 150.0
    latency_consecutive: int = 3
    latency_clear_consecutive: int = 3

    @classmethod
    def from_dict(cls, d: dict) -> "Thresholds":
        """Build from the chaos harness's thresholds JSON (chaos/chaos.json) or
        a persisted trigger_config blob."""
        def num(k, default):
            return float(d.get(k, default))

        def cnt(k, default):
            return int(num(k, default))

        return cls(
            oom_ram_pct=num("oom_ram_pct", 95.0),
            oom_clear_pct=num("oom_clear_pct", 90.0),
            oom_consecutive=cnt("oom_consecutive", 2),
            oom_clear_consecutive=cnt("oom_clear_consecutive", 3),
            disk_fail_consecutive=cnt("disk_fail_consecutive", 1),
            disk_fail_clear_consecutive=cnt("disk_fail_clear_consecutive", 3),
            disk_free_pct=num("disk_free_pct", 5.0),
            disk_free_clear_pct=num("disk_free_clear_pct", 15.0),
            disk_free_consecutive=cnt("disk_free_consecutive", 3),
            disk_write_latency_ms=num("disk_write_latency_ms", 50.0),
            disk_write_latency_clear_ms=num("disk_write_latency_clear_ms", 30.0),
            disk_latency_consecutive=cnt("disk_latency_consecutive", 5),
            disk_free_slope_bps=num("disk_free_slope_bps", -5_000_000.0),
            disk_clear_consecutive=cnt("disk_clear_consecutive", 3),
            latency_ms=num("latency_ms", 250.0),
            latency_clear_ms=num("latency_clear_ms", 150.0),
            latency_consecutive=cnt("latency_consecutive", 3),
            latency_clear_consecutive=cnt("latency_clear_consecutive", 3),
        )

    def to_dict(self) -> dict:
        """Serializable form for durable storage (trigger_config.config_json)."""
        return {
            "oom_ram_pct": self.oom_ram_pct,
            "oom_clear_pct": self.oom_clear_pct,
            "oom_consecutive": self.oom_consecutive,
            "oom_clear_consecutive": self.oom_clear_consecutive,
            "disk_fail_consecutive": self.disk_fail_consecutive,
            "disk_fail_clear_consecutive": self.disk_fail_clear_consecutive,
            "disk_free_pct": self.disk_free_pct,
            "disk_free_clear_pct": self.disk_free_clear_pct,
            "disk_free_consecutive": self.disk_free_consecutive,
            "disk_write_latency_ms": self.disk_write_latency_ms,
            "disk_write_latency_clear_ms": self.disk_write_latency_clear_ms,
            "disk_latency_consecutive": self.disk_latency_consecutive,
            "disk_free_slope_bps": self.disk_free_slope_bps,
            "disk_clear_consecutive": self.disk_clear_consecutive,
            "latency_ms": self.latency_ms,
            "latency_clear_ms": self.latency_clear_ms,
            "latency_consecutive": self.latency_consecutive,
            "latency_clear_consecutive": self.latency_clear_consecutive,
        }


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def ram_pct(sample: dict) -> float:
    total = sample.get("ram_total_bytes")
    used = sample.get("ram_used_bytes")
    if _num(total) and _num(used) and total > 0:
        return used / total * 100.0
    return 0.0


def _disk_entries(sample: dict) -> list[dict]:
    d = sample.get("disk")
    return [e for e in d if isinstance(e, dict)] if isinstance(d, list) else []


def min_disk_free_pct(sample: dict) -> float | None:
    pcts = []
    for e in _disk_entries(sample):
        total = e.get("total_bytes")
        free = e.get("free_bytes")
        if _num(total) and _num(free) and total > 0:
            pcts.append(free / total * 100.0)
    return min(pcts) if pcts else None


def max_disk_write_latency_ms(sample: dict) -> float:
    lats = [e["write_latency_ms"] for e in _disk_entries(sample)
            if _num(e.get("write_latency_ms"))]
    return max(lats) if lats else 0.0


def min_disk_free_bytes(sample: dict) -> float | None:
    frees = [e["free_bytes"] for e in _disk_entries(sample) if _num(e.get("free_bytes"))]
    return min(frees) if frees else None


def any_disk_mount_ro(sample: dict) -> bool:
    return any(e.get("mount_ro") is True for e in _disk_entries(sample))


def net_latency_ms(sample: dict) -> float:
    n = sample.get("net")
    if isinstance(n, list) and n and isinstance(n[0], dict):
        v = n[0].get("latency_ms")
        if _num(v):
            return float(v)
    return 0.0


def longest_run(samples: list[dict], pred) -> tuple[int, list[dict]]:
    """Return (length, samples) of the longest consecutive run satisfying pred."""
    best_len = best_run = 0
    cur = []
    best = []
    for s in samples:
        if pred(s):
            cur.append(s)
            best_run += 1
            if best_run > best_len:
                best_len = best_run
                best = list(cur)
        else:
            cur = []
            best_run = 0
    return best_len, best


def _slope(samples: list[dict], extract) -> float | None:
    """Bytes/sec slope of `extract(sample)` across the window (endpoint fit)."""
    pts = []
    for s in samples:
        v = extract(s)
        try:
            t = parse_rfc3339_epoch(s["ts"])
        except (KeyError, ValueError):
            continue
        if v is not None:
            pts.append((t, v))
    if len(pts) < 2:
        return None
    (t0, v0), (t1, v1) = pts[0], pts[-1]
    dt = t1 - t0
    if dt <= 0:
        return None
    return (v1 - v0) / dt


# --------------------------------------------------------------------------- #
# Per-rule breach / recovery predicates (used by both the stateless `evaluate`
# and the stateful `triggers.TriggerEngine`).
# --------------------------------------------------------------------------- #
RULE_IDS = ("oom-guard-01", "disk-fail-01", "disk-degrade-01", "latency-01")


def breach_run(samples_asc: list[dict], th: Thresholds) -> dict:
    """Longest consecutive breach run per rule, as {rule_id: (run_len, tail)}.

    The `tail` is the list of samples forming that longest run, oldest first.
    """
    oom_run, oom_tail = longest_run(
        samples_asc, lambda s: ram_pct(s) >= th.oom_ram_pct
    )
    ro_run, ro_tail = longest_run(samples_asc, any_disk_mount_ro)

    free_run, free_tail = longest_run(
        samples_asc, lambda s: (min_disk_free_pct(s) is not None
                                and min_disk_free_pct(s) <= th.disk_free_pct)
    )
    lat_run, lat_tail = longest_run(
        samples_asc, lambda s: max_disk_write_latency_ms(s) >= th.disk_write_latency_ms
    )
    slope = _slope(samples_asc, min_disk_free_bytes)
    # A steep negative slope is a window-aggregate signal (endpoint fit), so it is
    # already smoothed; treat it as a full-window run so it can fire on its own.
    slope_run = len(samples_asc) if (slope is not None and slope <= th.disk_free_slope_bps) else 0

    # disk-degrade: the strongest of its three sub-signals wins for the run.
    candidates = [(free_run, free_tail), (lat_run, lat_tail)]
    if slope_run > 0:
        candidates.append((slope_run, list(samples_asc)))
    disk_run, disk_tail = max(candidates, key=lambda pair: pair[0])

    lat2_run, lat2_tail = longest_run(
        samples_asc, lambda s: net_latency_ms(s) >= th.latency_ms
    )

    return {
        "oom-guard-01": (oom_run, oom_tail),
        "disk-fail-01": (ro_run, ro_tail),
        "disk-degrade-01": (disk_run, disk_tail),
        "latency-01": (lat2_run, lat2_tail),
    }


def breach_streak(samples_asc: list[dict], th: Thresholds) -> dict:
    """The *current* consecutive breaching streak at the tail of the window, as
    {rule_id: [breaching samples, oldest first]}.

    This is the stateful engine's fire condition: a trigger fires only when the
    metric is *currently* breached for `*_consecutive` samples (counting back
    from the newest sample), not merely at some point in the window. It is what
    lets a recovered metric stay re-armed instead of re-firing on the historical
    breach samples still inside the window.

    disk-degrade uses its per-sample sub-signals (free floor / write latency);
    the slope signal is window-aggregate and is therefore only in `breach_run`
    (used by the stateless probe), not here.
    """
    preds = {
        "oom-guard-01": lambda s: ram_pct(s) >= th.oom_ram_pct,
        "disk-fail-01": any_disk_mount_ro,
        "disk-degrade-01": lambda s: (
            (min_disk_free_pct(s) is not None and min_disk_free_pct(s) <= th.disk_free_pct)
            or max_disk_write_latency_ms(s) >= th.disk_write_latency_ms
        ),
        "latency-01": lambda s: net_latency_ms(s) >= th.latency_ms,
    }
    out: dict = {}
    for rule_id, pred in preds.items():
        streak: list[dict] = []
        for s in reversed(samples_asc):
            if pred(s):
                streak.append(s)
            else:
                break
        streak.reverse()
        out[rule_id] = streak
    return out


def recovered(samples_asc: list[dict], th: Thresholds) -> dict:
    """True per rule when the metric has returned to a healthy band for the last
    `*_clear_consecutive` samples — the hysteresis re-arm condition."""
    if not samples_asc:
        return {r: False for r in RULE_IDS}

    def tail_all(pred, n) -> bool:
        return len(samples_asc) >= n and all(pred(s) for s in samples_asc[-n:])

    return {
        "oom-guard-01": tail_all(
            lambda s: ram_pct(s) <= th.oom_clear_pct, th.oom_clear_consecutive
        ),
        "disk-fail-01": tail_all(
            lambda s: bool(_disk_entries(s)) and not any_disk_mount_ro(s),
            th.disk_fail_clear_consecutive,
        ),
        "disk-degrade-01": tail_all(
            lambda s: (
                (min_disk_free_pct(s) is not None
                 and min_disk_free_pct(s) >= th.disk_free_clear_pct)
                and max_disk_write_latency_ms(s) <= th.disk_write_latency_clear_ms
            ),
            th.disk_clear_consecutive,
        ),
        "latency-01": tail_all(
            lambda s: net_latency_ms(s) <= th.latency_clear_ms, th.latency_clear_consecutive
        ),
    }


def _rule_meta(rule_id: str) -> tuple[str, str]:
    """(type, metric) per frozen rule id."""
    return {
        "oom-guard-01": ("oom", "ram_used_bytes"),
        "disk-fail-01": ("disk_fail", "disk_mount_ro"),
        "disk-degrade-01": ("disk_degrade", "disk_free_bytes"),
        "latency-01": ("latency", "net_latency_ms"),
    }[rule_id]


def _threshold_desc(rule_id: str, th: Thresholds) -> str:
    return {
        "oom-guard-01": (f">= {th.oom_ram_pct:.0f}% of ram_total_bytes for "
                         f"{th.oom_consecutive} consecutive samples"),
        "disk-fail-01": "any disk[].mount_ro == true (EXT4 remount read-only)",
        "disk-degrade-01": (f"free <= {th.disk_free_pct:.0f}% OR write_latency_ms "
                            f">= {th.disk_write_latency_ms:.0f} OR free slope <= "
                            f"{th.disk_free_slope_bps} bytes/s"),
        "latency-01": (f"net[].latency_ms >= {th.latency_ms:.0f} ms for "
                       f"{th.latency_consecutive} consecutive samples"),
    }[rule_id]


def _value(rule_id: str, tail: list[dict], th: Thresholds) -> float | None:
    if not tail:
        return None
    if rule_id == "oom-guard-01":
        return max(ram_pct(s) for s in tail)
    if rule_id == "disk-fail-01":
        return 1.0
    if rule_id == "latency-01":
        return max(net_latency_ms(s) for s in tail)
    # disk-degrade: report the strongest of its sub-signals.
    frees = [min_disk_free_pct(s) for s in tail if min_disk_free_pct(s) is not None]
    lats = [max_disk_write_latency_ms(s) for s in tail]
    free = min(frees) if frees else None
    lat = max(lats) if lats else None
    slope = _slope(tail, min_disk_free_bytes)
    if free is not None and free <= th.disk_free_pct:
        return free
    if lat is not None and lat >= th.disk_write_latency_ms:
        return lat
    return slope


def build_event(rule_id: str, node_id: str, tail: list[dict], th: Thresholds,
                now_iso: str, breach_started_at: str | None = None) -> HealthEvent:
    type_, metric = _rule_meta(rule_id)
    return HealthEvent(
        event_id=ulid.new_ulid(),
        node_id=node_id,
        rule_id=rule_id,
        type=type_,
        metric=metric,
        value=_value(rule_id, tail, th),
        threshold=_threshold_desc(rule_id, th),
        fired_at=now_iso,
        evidence=list(tail),
        breach_started_at=breach_started_at,
    )


def evaluate(node_id: str, samples_asc: list[dict], th: Thresholds, now_iso: str) -> list[HealthEvent]:
    """Stateless evaluation over an oldest-first window: fire any rule whose
    breach run reaches its debounce count, as if from a cold (armed) start.

    The production loop does NOT use this directly — it goes through the
    stateful `TriggerEngine` (see operator/triggers.py), which adds hysteresis
    and persistence. This function remains for standalone/probe use and for the
    detector's unit tests.
    """
    events: list[HealthEvent] = []
    if not samples_asc:
        return events
    runs = breach_run(samples_asc, th)
    need = {
        "oom-guard-01": th.oom_consecutive,
        "disk-fail-01": th.disk_fail_consecutive,
        "disk-degrade-01": min(th.disk_free_consecutive, th.disk_latency_consecutive),
        "latency-01": th.latency_consecutive,
    }
    for rule_id in RULE_IDS:
        run, tail = runs[rule_id]
        if run >= need[rule_id]:
            first_ts = tail[0].get("ts") if tail else None
            events.append(build_event(rule_id, node_id, tail, th, now_iso, first_ts))
    return events
