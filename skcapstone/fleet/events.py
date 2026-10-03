"""Append-only, bounded, per-node event log (spec 3.5).

One rotating JSONL file per node, never per object and never per event.
Events are causal history for the cognitive layer; they are observability,
not control flow: no controller may key a decision off this log.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from datetime import datetime, timezone

from .paths import FleetPaths
from .store import OwnershipError, Writer

MAX_BYTES = 1_048_576
DEDUPE_WINDOW_S = 300.0

_last_emit: dict[tuple[str, str, str, str, str], float] = {}


def reset_dedupe() -> None:
    """Clear the in-process dedupe memory (tests, daemon restart)."""
    _last_emit.clear()


def emit(
    paths: FleetPaths,
    writer: Writer,
    *,
    kind: str,
    name: str,
    type: str,
    reason: str,
    message: str,
    now: float | None = None,
) -> bool:
    """Append one event to this node's log; False when deduped.

    Flood-safe (R2): rate-capped by the dedupe window, bounded by size
    rotation, serialized by a local flock so same-node processes share
    one file while the single-writer-per-node invariant holds for sync.
    """
    if writer.role not in {"sknoded", "controller", "scheduler"}:
        raise OwnershipError(f"role {writer.role!r} may not emit events")
    ts = time.time() if now is None else now
    key = (writer.node, kind, name, type, reason)
    last = _last_emit.get(key)
    if last is not None and ts - last < DEDUPE_WINDOW_S:
        return False
    _last_emit[key] = ts
    line = json.dumps(
        {
            "ts": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "node": writer.node,
            "kind": kind,
            "name": name,
            "type": type,
            "reason": reason,
            "message": message,
            "count": 1,
        },
        sort_keys=True,
    )
    path = paths.events_path(writer.node)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(".events.lock")
    with open(lock_path, "w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if path.exists() and path.stat().st_size >= MAX_BYTES:
            os.replace(path, path.with_name("events.jsonl.1"))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return True


def read(
    paths: FleetPaths,
    node: str,
    *,
    kind: str | None = None,
    name: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Read events for a node, oldest first, filtered by kind/name."""
    out: list[dict] = []
    live = paths.events_path(node)
    for p in (live.with_name("events.jsonl.1"), live):
        if not p.exists():
            continue
        for raw in p.read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if kind is not None and ev.get("kind") != kind:
                continue
            if name is not None and ev.get("name") != name:
                continue
            out.append(ev)
    return out[-limit:]
