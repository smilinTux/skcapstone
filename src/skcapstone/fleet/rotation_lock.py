"""Bounded fair ownership for the shared fleet mutation lock."""

from __future__ import annotations

import fcntl
import time
from pathlib import Path
from typing import TextIO


def acquire_rotation_lock(
    path: Path,
    *,
    seat: str,
    wait_seconds: float = 240,
    poll_seconds: float = 0.1,
) -> TextIO | None:
    """Acquire the exclusive lock, giving only Niobe a bounded wait."""

    lock = Path(path).open("w", encoding="utf-8")
    deadline = time.monotonic() + (max(0, wait_seconds) if seat == "niobe" else 0)
    while True:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                lock.close()
                return None
            time.sleep(min(poll_seconds, remaining))
