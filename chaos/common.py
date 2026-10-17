"""Shared helpers for the Termini chaos/fault-injection harness (stdlib only).

Nothing here depends on third-party packages. The ULID scheme is vendored from
`controller/app/ulid.py` (frozen by docs/contracts/controller-api.md §1.3) so the
harness can generate identifiers the controller accepts without importing the
controller package across directory boundaries.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import time
import urllib.error
import urllib.request
from typing import Any

# --------------------------------------------------------------------------- #
# ULID — Crockford base32, 26 chars, uppercase (no I/L/O/U).
# --------------------------------------------------------------------------- #
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ENC = {ch: i for i, ch in enumerate(CROCKFORD)}
_last_ms = -1


def _encode(val: int, n_chars: int) -> str:
    out = ""
    for _ in range(n_chars):
        out = CROCKFORD[val & 0x1F] + out
        val >>= 5
    return out


def new_ulid() -> str:
    """A fresh, monotonic, time-ordered ULID (matches controller/app/ulid.py)."""
    global _last_ms
    ms = int(time.time() * 1000)
    if ms <= _last_ms:
        ms = _last_ms + 1
    _last_ms = ms
    rand = int.from_bytes(os.urandom(10), "big") & ((1 << 80) - 1)
    return _encode(ms, 10) + _encode(rand, 16)


def is_ulid(s: object) -> bool:
    return isinstance(s, str) and len(s) == 26 and all(ch in _ENC for ch in s)


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #
def rfc3339_now() -> str:
    """Current UTC time as RFC3339 with millisecond precision (schema §2)."""
    dt = datetime.datetime.now(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# --------------------------------------------------------------------------- #
# HTTP (stdlib urllib); returns (status, decoded_body_or_text)
# --------------------------------------------------------------------------- #
def http_json(
    method: str,
    url: str,
    body: Any = None,
    token: str | None = None,
    timeout: float = 15.0,
) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, (json.loads(raw) if raw else None)
        except json.JSONDecodeError:
            return e.code, raw.decode(errors="replace")


def wait_for_http(url: str, attempts: int = 100, delay: float = 0.1) -> bool:
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                r.read(1)
                return True
        except urllib.error.HTTPError:
            return True  # any HTTP response means the socket is up
        except Exception:
            time.sleep(delay)
    return False


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #
def read_json(path: str | os.PathLike) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str | os.PathLike, obj: Any) -> None:
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")


def append_jsonl(path: str | os.PathLike, obj: Any) -> None:
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj) + "\n")


def read_jsonl(path: str | os.PathLike) -> list[Any]:
    p = pathlib.Path(path)
    if not p.exists():
        return []
    out = []
    with open(p, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# --------------------------------------------------------------------------- #
# Threshold evaluation — mirrors the frozen rules in
# docs/contracts/node-telemetry.md §5 (demo, frozen).
# --------------------------------------------------------------------------- #
def _ram_pct(sample: dict) -> float | None:
    total = sample.get("ram_total_bytes")
    used = sample.get("ram_used_bytes")
    if isinstance(total, (int, float)) and total and isinstance(used, (int, float)):
        return used / total * 100.0
    return None


def _disk_first(sample: dict) -> dict:
    d = sample.get("disk")
    return d[0] if isinstance(d, list) and d and isinstance(d[0], dict) else {}


def _longest_run(series: list[dict], predicate) -> int:
    best = run = 0
    for s in series:
        if predicate(s):
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def evaluate_series(samples: list[dict], thresholds: dict) -> dict:
    """Evaluate the demo threshold rules over an ascending sample series.

    Only the two demo faults are evaluated here (`oom`, `disk-fail`). The
    `disk-degrade-01` and `latency-01` detector rules remain implemented in
    `controller/app/operator/detect.py` (a real customer node can still produce
    those signals), but the demo harness can no longer inject them.

    Returns {rule_id: {"fired": bool, "run": int, "needed": int}}.
    """
    def num(key: str, default: float) -> float:
        v = thresholds.get(key, default)
        return float(v)

    oom_run = _longest_run(samples, lambda s: (_ram_pct(s) or 0.0) >= num("oom_ram_pct", 95.0))
    mount_ro_run = _longest_run(samples, lambda s: _disk_first(s).get("mount_ro") is True)

    return {
        "oom-guard-01": {
            "fired": oom_run >= int(num("oom_consecutive", 2)),
            "run": oom_run,
            "needed": int(num("oom_consecutive", 2)),
        },
        "disk-fail-01": {
            "fired": mount_ro_run >= int(num("disk_fail_consecutive", 1)),
            "run": mount_ro_run,
            "needed": int(num("disk_fail_consecutive", 1)),
        },
    }
