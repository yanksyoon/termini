#!/usr/bin/env python3
"""gen_ulid.py — print one valid ULID (stdlib only)."""
import os
import time

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ts = int(time.time() * 1000) & ((1 << 48) - 1)
rnd = int.from_bytes(os.urandom(10), "big")
n = (ts << 80) | rnd
out = []
for _ in range(26):
    out.append(CROCKFORD[n & 0x1F])
    n >>= 5
print("".join(reversed(out)))
