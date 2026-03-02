"""Lightweight in-process activity bus for the SKCapstone daemon.

Stores the last 100 events in a thread-safe deque and fans out live
events to registered SSE client queues.  No external dependencies —
stdlib only.

Usage::

    from . import activity

    # publish an event (any thread)
    activity.push("memory.stored", {"memory_id": "abc", "layer": "short-term"})

    # SSE handler: register a queue, drain history, then block on live events
    q = queue.Queue(maxsize=200)
    activity.register_client(q)
    try:
        for chunk in activity.get_history_encoded():
            wfile.write(chunk)
        while True:
            chunk = q.get(timeout=15)   # raises queue.Empty on timeout
            wfile.write(chunk)
    finally:
        activity.unregister_client(q)
"""

from __future__ import annotations

import json
import queue
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

_MAXLEN = 100

_history: deque[dict] = deque(maxlen=_MAXLEN)
_history_lock = threading.Lock()

_clients: set[queue.Queue] = set()
_clients_lock = threading.Lock()


def push(event_type: str, data: dict[str, Any]) -> None:
    """Append an event to history and fan out to all live SSE clients.

    Args:
        event_type: Dot-namespaced event type, e.g. ``"memory.stored"``.
        data: Arbitrary JSON-serialisable payload dict.
    """
    event: dict = {
        "type": event_type,
        "ts": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    with _history_lock:
        _history.append(event)
    _fan_out(event)


def get_history() -> list[dict]:
    """Return a snapshot of the last ≤100 events (oldest first)."""
    with _history_lock:
        return list(_history)


def get_history_encoded() -> list[bytes]:
    """Return history as a list of SSE-encoded byte chunks."""
    return [_encode(e) for e in get_history()]


def register_client(q: queue.Queue) -> None:
    """Register a queue to receive live SSE byte chunks."""
    with _clients_lock:
        _clients.add(q)


def unregister_client(q: queue.Queue) -> None:
    """Remove a queue from the live fan-out set."""
    with _clients_lock:
        _clients.discard(q)


# ── internal helpers ──────────────────────────────────────────────────────────

def _fan_out(event: dict) -> None:
    global _clients
    chunk = _encode(event)
    dead: set[queue.Queue] = set()
    with _clients_lock:
        clients = set(_clients)
    for q in clients:
        try:
            q.put_nowait(chunk)
        except Exception:
            dead.add(q)
    if dead:
        with _clients_lock:
            _clients -= dead


def _encode(event: dict) -> bytes:
    data = json.dumps(event, default=str)
    return f"data: {data}\n\n".encode("utf-8")
