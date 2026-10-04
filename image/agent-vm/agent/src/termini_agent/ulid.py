"""Minimal ULID generator (Crockford base32, 48-bit time + 80-bit randomness).

Kept dependency-free so it works with the frozen stdlib and stays byte-compatible
with the controller's ULIDs (controller/app/ulid.py).
"""

from __future__ import annotations

import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        out.append(_CROCKFORD[rem])
    return "".join(reversed(out))


def new_ulid() -> str:
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")
    return _encode(ts, 10) + _encode(rand, 16)
