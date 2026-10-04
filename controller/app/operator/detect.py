"""Threshold detector — pure functions that turn a telemetry window into
structured HealthEvents. No I/O, no side effects: the loop feeds it samples
read from the store and consumes the events it returns.

Detection follows docs/contracts/node-telemetry.md §5 (frozen demo rules) and the
chaos harness's evidence (`chaos/chaos.json`), which drive the same signals:

  oom          ram_used_bytes/ram_total_bytes >= oom_ram_pct for oom_consecutive
               consecutive samples                          -> oom-guard-01
  disk_degrade any of: free/total <= disk_free_pct for disk_free_consecutive,
               OR disk[].write_latency_ms >= disk_write_latency_ms for
               disk_latency_consecutive, OR free_bytes falling faster than
               disk_free_slope_bps (steep negative slope)    -> disk-degrade-01
  latency      net[].latency_ms >= latency_ms for latency_consecutive
               consecutive samples                          -> latency-01

"Sustained" means a run of consecutive samples of at least the required length
(same semantics as the chaos harness's longest-run evaluation). Thresholds come
from config (env), never from code — see `Thresholds`; the standalone operator
CLI constructs one directly from the harness's `--thresholds` JSON.

Note on p95: the frozen telemetry schema has no application p95 field — the only
latency signal is `net[].latency_ms` (see `chaos/README.md`). The latency rule
therefore thresholds that field; adding an application p95 would require a new
C0 field in `governance.md` §4.1 first.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import ulid
from ..validate import parse_rfc3339_epoch
from .types import HealthEvent


@dataclass(frozen=True)
class Thresholds:
    oom_ram_pct: float = 95.0
    oom_consecutive: int = 2
    disk_free_pct: float = 5.0
    disk_free_consecutive: int = 3
    disk_write_latency_ms: float = 50.0
    disk_latency_consecutive: int = 5
    disk_free_slope_bps: float = -5_000_000.0   # bytes/sec (negative => falling)
    latency_ms: float = 250.0
    latency_consecutive: int = 3

    @classmethod
    def from_dict(cls, d: dict) -> "Thresholds":
        """Build from the chaos harness's thresholds JSON (chaos/chaos.json)."""
        def num(k, default):
            v = d.get(k, default)
            return float(v)

        def cnt(k, default):
            return int(num(k, default))

        return cls(
            oom_ram_pct=num("oom_ram_pct", 95.0),
            oom_consecutive=cnt("oom_consecutive", 2),
            disk_free_pct=num("disk_free_pct", 5.0),
            disk_free_consecutive=cnt("disk_free_consecutive", 3),
            disk_write_latency_ms=num("disk_write_latency_ms", 50.0),
            disk_latency_consecutive=cnt("disk_latency_consecutive", 5),
            disk_free_slope_bps=num("disk_free_slope_bps", -5_000_000.0),
            latency_ms=num("latency_ms", 250.0),
            latency_consecutive=cnt("latency_consecutive", 3),
        )


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def ram_pct(sample: dict) -> float | None:
    total = sample.get("ram_total_bytes")
    used = sample.get("ram_used_bytes")
    if _num(total) and _num(used) and total > 0:
        return used / total * 100.0
    return None


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


def max_disk_write_latency_ms(sample: dict) -> float | None:
    lats = [e["write_latency_ms"] for e in _disk_entries(sample)
            if _num(e.get("write_latency_ms"))]
    return max(lats) if lats else None


def min_disk_free_bytes(sample: dict) -> float | None:
    frees = [e["free_bytes"] for e in _disk_entries(sample) if _num(e.get("free_bytes"))]
    return min(frees) if frees else None


def net_latency_ms(sample: dict) -> float | None:
    n = sample.get("net")
    if isinstance(n, list) and n and isinstance(n[0], dict):
        v = n[0].get("latency_ms")
        return float(v) if _num(v) else None
    return None


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


def evaluate(node_id: str, samples_asc: list[dict], th: Thresholds, now_iso: str) -> list[HealthEvent]:
    """Evaluate all three conditions over an oldest-first sample window and
    return the HealthEvents that fired (at most one per condition per call)."""
    events: list[HealthEvent] = []
    if not samples_asc:
        return events

    # --- OOM: sustained mem% above threshold ---------------------------------
    run, tail = longest_run(samples_asc, lambda s: (ram_pct(s) or 0.0) >= th.oom_ram_pct)
    if run >= th.oom_consecutive:
        val = max(ram_pct(s) or 0.0 for s in tail)
        events.append(HealthEvent(
            event_id=ulid.new_ulid(), node_id=node_id,
            rule_id="oom-guard-01", type="oom", metric="ram_used_bytes",
            value=val,
            threshold=f">= {th.oom_ram_pct:.0f}% of ram_total_bytes for "
                      f"{th.oom_consecutive} consecutive samples",
            fired_at=now_iso, evidence=list(tail),
        ))

    # --- disk: free low OR write-latency high OR steep negative slope ---------
    free_run, free_tail = longest_run(
        samples_asc, lambda s: (min_disk_free_pct(s) is not None
                                and min_disk_free_pct(s) <= th.disk_free_pct)
    )
    lat_run, lat_tail = longest_run(
        samples_asc, lambda s: (max_disk_write_latency_ms(s) is not None
                                and max_disk_write_latency_ms(s) >= th.disk_write_latency_ms)
    )
    slope = _slope(samples_asc, min_disk_free_bytes)

    value: float | None = None
    threshold: str = ""
    evidence: list[dict] | None = None
    if free_run >= th.disk_free_consecutive:
        val = min(min_disk_free_pct(s) or 0.0 for s in free_tail)
        threshold = (f"free <= {th.disk_free_pct:.0f}% of total for "
                     f"{th.disk_free_consecutive} consecutive samples")
        evidence = free_tail
        value = val
    elif lat_run >= th.disk_latency_consecutive:
        value = max(max_disk_write_latency_ms(s) or 0.0 for s in lat_tail)
        threshold = (f"write_latency_ms >= {th.disk_write_latency_ms:.0f} for "
                     f"{th.disk_latency_consecutive} consecutive samples")
        evidence = lat_tail
    elif slope is not None and slope <= th.disk_free_slope_bps:
        value = slope
        threshold = f"free_bytes slope <= {th.disk_free_slope_bps} bytes/s"
        evidence = list(samples_asc)

    if evidence:
        events.append(HealthEvent(
            event_id=ulid.new_ulid(), node_id=node_id,
            rule_id="disk-degrade-01", type="disk_degrade", metric="disk_free_bytes",
            value=value, threshold=threshold, fired_at=now_iso,
            evidence=list(evidence),
        ))

    # --- latency: sustained net latency above threshold -----------------------
    run, tail = longest_run(samples_asc, lambda s: (net_latency_ms(s) is not None
                                                    and net_latency_ms(s) >= th.latency_ms))
    if run >= th.latency_consecutive:
        val = max(net_latency_ms(s) or 0.0 for s in tail)
        events.append(HealthEvent(
            event_id=ulid.new_ulid(), node_id=node_id,
            rule_id="latency-01", type="latency", metric="net_latency_ms",
            value=val,
            threshold=f"net[].latency_ms >= {th.latency_ms:.0f} ms for "
                      f"{th.latency_consecutive} consecutive samples",
            fired_at=now_iso, evidence=list(tail),
        ))

    return events
