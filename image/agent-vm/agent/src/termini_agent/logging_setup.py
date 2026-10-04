"""Local-only JSONL logging (agent-runtime.md §5.2 step 4, §13.1).

Every line is a single JSON object. Higher-classification content (task bodies,
model output at C1+) is never written here — only C0 counters and identifiers.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from pathlib import Path


class JsonlFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        base = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "termini", None)
        if isinstance(extra, dict):
            base.update(extra)
        if record.exc_info:
            base["exc"] = self.formatException(record.exc_info)
        return json.dumps(base, separators=(",", ":"), default=str)


def configure_logging(sink: str, *, also_stderr: bool = False, level: int = logging.INFO) -> Path:
    """Point the root logger at a JSONL sink. Falls back to stderr if the sink is
    not writable (e.g. running the test suite outside the image)."""
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    target = Path(sink)
    handler: logging.Handler
    if _writable(target):
        handler = logging.FileHandler(target, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonlFormatter())
    root.addHandler(handler)

    if also_stderr and not isinstance(handler, logging.StreamHandler):
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(JsonlFormatter())
        root.addHandler(sh)

    return target


def _writable(target: Path) -> bool:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8"):
            return True
    except OSError:
        return False


_log_lock = threading.Lock()


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
