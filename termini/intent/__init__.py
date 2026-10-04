"""Public API for termini.intent.

Exposes the extractor and validator as importable modules and the top-level
``extract_and_validate`` convenience. The deterministic clamp validator is the
security boundary; nothing downstream should import or call the raw extractor
output except through :func:`extract_and_validate`.
"""

from .extractor import ExtractionError, extract, extract_and_validate
from .types import (
    DEFAULT_HOST_CAPS,
    AllocationSpec,
    ClampDecision,
    HostCaps,
    Rejection,
    RoleCaps,
    ValidationResult,
)
from .validator import clamp_validate

__all__ = [
    "AllocationSpec",
    "ClampDecision",
    "DEFAULT_HOST_CAPS",
    "ExtractionError",
    "HostCaps",
    "Rejection",
    "RoleCaps",
    "ValidationResult",
    "clamp_validate",
    "extract",
    "extract_and_validate",
]
