"""Host-local terminal capacity publication.

Snapshots are shared coordination evidence, not CardStore records. Updates use a
lock and atomic replacement so concurrent workers cannot erase one another.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def invalidate_worker(path: str | Path, host: str, card: str) -> dict[str, Any]:
    """Remove exactly *card* from a host snapshot and atomically publish it.

    The lock covers read/modify/replace, which is essential when two workers
    terminate in the same rotation cycle. Other cards and lane accounting are
    retained, and an invalidation marker makes stale snapshots observable.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        import fcntl
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        snapshot = _load(target)
        cards = [str(item) for item in snapshot.get("cards", []) if str(item) != str(card)]
        snapshot["host"] = host
        snapshot["cards"] = cards
        snapshot["invalidated_card"] = str(card)
        snapshot["invalidated_at"] = __import__("time").time()
        payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return snapshot
