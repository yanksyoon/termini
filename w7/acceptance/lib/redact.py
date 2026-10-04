"""Redact secrets before they reach an evidence artifact or summary.

Any dict value whose key contains ``token``, ``secret``, ``password``,
``private_key``, or ``authorization`` is replaced with ``<redacted>``, so a
check that echoes a controller response never leaks a bearer/node token into a
committed artifact.
"""
from __future__ import annotations

_REDACT_KEYS = ("token", "secret", "password", "private_key", "authorization",
                "client_cert", "ca_chain", "join_token")


def redact(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(t in k.lower() for t in _REDACT_KEYS):
                out[k] = "<redacted>"
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj
