"""In-process event broker (thread-safe pub/sub).

One broker instance lives on the controller server and fans out events to every
active subscriber: the unified WebSocket stream (`/stream/v1`), the SSE log
stream (`/logs`), and any future consumers (AI-operator loop tap, audit).

Event shape (controller-api.md §7.1 / dashboard.md §2.2):

    {"event": "<name>", "ts": "<RFC3339>", "project_id": "<ULID>",
     "data": { ... }}

Subscribers are per-connection queues; a slow subscriber is shed rather than
allowed to block the publisher (bounded queue, drop-oldest).
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any


def _now_rfc3339() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class EventBroker:
    """Fan-out of named events to registered subscriber queues."""

    def __init__(self, max_queue: int = 4096) -> None:
        self._lock = threading.Lock()
        self._subs: set[queue.Queue] = set()
        self._max_queue = max_queue

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._max_queue)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: str, data: Any, project_id: str) -> None:
        msg = {"event": event, "ts": _now_rfc3339(), "project_id": project_id, "data": data}
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                # Shed the oldest event so a slow subscriber still gets the
                # newest state rather than blocking the publisher.
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)
