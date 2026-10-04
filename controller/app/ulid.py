"""ULID generation — 26 chars of Crockford base32.

Implements the identifier scheme fixed in controller-api.md §1.3 and
governance.md §4: uppercase, alphabet `0-9A-HJKMNP-TV-Z` (no I/L/O/U).

Timestamps are monotonic within a process (the 48-bit time field is bumped if
needed), so ULIDs sort by creation time.
"""
from __future__ import annotations

import os
import time

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # excludes I, L, O, U
_ENCODING = {ch: i for i, ch in enumerate(CROCKFORD)}

_MAX_TIME = (1 << 48) - 1
_MAX_RAND = (1 << 80) - 1


def _encode_time(ms: int) -> str:
    s = ""
    v = ms
    for _ in range(10):
        s = CROCKFORD[v & 0x1F] + s
        v >>= 5
    return s


def _encode_random(r: int) -> str:
    s = ""
    v = r
    for _ in range(16):
        s = CROCKFORD[v & 0x1F] + s
        v >>= 5
    return s


_last_ms = -1


def new_ulid() -> str:
    """Return a fresh, time-ordered ULID string."""
    global _last_ms
    ms = int(time.time() * 1000)
    if ms <= _last_ms:
        ms = _last_ms + 1
    if ms > _MAX_TIME:
        raise OverflowError("ULID time field exhausted")
    _last_ms = ms
    rand = int.from_bytes(os.urandom(10), "big") & _MAX_RAND
    return _encode_time(ms) + _encode_random(rand)


def is_valid_ulid(s: object) -> bool:
    if not isinstance(s, str) or len(s) != 26:
        return False
    return all(ch in _ENCODING for ch in s)
