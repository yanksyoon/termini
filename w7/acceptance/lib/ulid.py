"""Independent ULID validator (Crockford base32, 26 chars, no I/L/O/U).

Reimplemented from the frozen contract (controller-api.md §1.3) rather than
importing the controller's own ``app.ulid``, so pass/fail is determined by the
harness, not by the system under test.
"""
from __future__ import annotations

import random

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """Return a random syntactically-valid ULID (26 chars, Crockford base32).

    Only used to craft test payloads for the harness; ordering/monotonicity is
    not needed for that purpose.
    """
    return "".join(random.choice(_ALPHABET) for _ in range(26))


def is_valid_ulid(value: str) -> bool:
    if not isinstance(value, str):
        return False
    if len(value) != 26:
        return False
    for ch in value:
        if ch not in _ALPHABET:
            return False
    return True
