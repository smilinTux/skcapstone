"""Persistent error recovery queue for failed operations.

Operations that fail (LLM calls, message sends, sync failures) are
enqueued here and retried with exponential backoff up to MAX_RETRIES times.
The queue is persisted to ~/.skcapstone/error_queue.json.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("skcapstone.error_queue")

DEFAULT_QUEUE_PATH = Path("~/.skcapstone/error_queue.json")
MAX_RETRIES = 3
# Exponential backoff: delay = BASE_BACKOFF_SECONDS * 2^attempt (seconds)
BASE_BACKOFF_SECONDS = 60


class ErrorStatus(str, Enum):
    """Life-cycle status of a queued error entry."""

    PENDING = "pending"       # Waiting for next retry attempt
    RETRYING = "retrying"     # Retry in progress (transient; set during retry)
    EXHAUSTED = "exhausted"   # Max retries reached
    RESOLVED = "resolved"     # Successfully retried and recovered


class ErrorEntry:
    """One failed operation stored in the error queue.

    Attributes:
        entry_id: Unique identifier (UUID4 hex).
        operation_type: Logical type label, e.g. "llm_call", "message_send".
        payload: Arbitrary serialisable data needed to replay the operation.
        error_message: Human-readable description of the original failure.
        created_at: ISO-8601 UTC timestamp when the entry was created.
        retry_count: Number of retries attempted so far.
        next_retry_at: ISO-8601 UTC timestamp after which the next retry is due.
        status: Current :class:`ErrorStatus`.
    """

    def __init__(
        self,
        operation_type: str,
        payload: dict[str, Any],
        error_message: str,
        entry_id: Optional[str] = None,
        created_at: Optional[str] = None,
        retry_count: int = 0,
        next_retry_at: Optional[str] = None,
        status: str = ErrorStatus.PENDING,
    ) -> None:
        self.entry_id: str = entry_id or uuid.uuid4().hex
        self.operation_type: str = operation_type
        self.payload: dict[str, Any] = payload
        self.error_message: str = error_message
        self.created_at: str = created_at or _now_iso()
        self.retry_count: int = retry_count
        self.next_retry_at: Optional[str] = next_retry_at
        # Always store the bare string value, not the enum repr.
        self.status: str = status.value if isinstance(status, ErrorStatus) else str(status)

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict (JSON-safe).

        Returns:
            Dict representation of the entry.
        """
        return {
            "entry_id": self.entry_id,
            "operation_type": self.operation_type,
            "payload": self.payload,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "retry_count": self.retry_count,
            "next_retry_at": self.next_retry_at,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ErrorEntry":
        """Deserialise from a plain dict.

        Args:
            data: Dict previously produced by :meth:`to_dict`.

        Returns:
            Reconstructed :class:`ErrorEntry`.
        """
        return cls(
            entry_id=data["entry_id"],
            operation_type=data["operation_type"],
            payload=data.get("payload", {}),
            error_message=data.get("error_message", ""),
            created_at=data.get("created_at"),
            retry_count=data.get("retry_count", 0),
            next_retry_at=data.get("next_retry_at"),
            status=data.get("status", ErrorStatus.PENDING),
        )

    def __repr__(self) -> str:
        return (
            f"<ErrorEntry id={self.entry_id[:8]} op={self.operation_type} "
            f"retries={self.retry_count} status={self.status}>"
        )


class ErrorQueue:
    """Persistent queue for failed operations with exponential-backoff retry.

    The queue is stored as a JSON array in *path* (default
    ``~/.skcapstone/error_queue.json``).  All mutating methods persist
    immediately so the queue survives process restarts.

    Args:
        path: Path to the JSON file.  Expanded with :meth:`Path.expanduser`.
        max_retries: Maximum number of retry attempts before an entry is
            marked :attr:`ErrorStatus.EXHAUSTED`.
        base_backoff: Base delay in seconds for exponential backoff.

    Example::

        queue = ErrorQueue()
        queue.enqueue("llm_call", {"prompt": "..."}, "timeout")
        due = queue.due_entries()
        for entry in due:
            queue.retry(entry.entry_id, handler=my_llm_handler)
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        max_retries: int = MAX_RETRIES,
        base_backoff: int = BASE_BACKOFF_SECONDS,
    ) -> None:
        self._path: Path = (path or DEFAULT_QUEUE_PATH).expanduser()
        self._max_retries = max_retries
        self._base_backoff = base_backoff

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> list[ErrorEntry]:
        """Load all entries from disk.

        Returns:
            List of :class:`ErrorEntry` objects.
        """
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return [ErrorEntry.from_dict(d) for d in data]
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("error_queue: corrupt queue file (%s); starting fresh", exc)
            return []

    def _save(self, entries: list[ErrorEntry]) -> None:
        """Persist *entries* to disk atomically.

        Args:
            entries: Current list of queue entries to write.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps([e.to_dict() for e in entries], indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(
        self,
        operation_type: str,
        payload: dict[str, Any],
        error_message: str,
    ) -> ErrorEntry:
        """Add a new failed operation to the queue.

        Args:
            operation_type: Short label for the operation (e.g. ``"llm_call"``).
            payload: Data needed to re-run the operation.
            error_message: Human-readable description of the failure.

        Returns:
            The newly created :class:`ErrorEntry`.
        """
        entries = self._load()
        entry = ErrorEntry(
            operation_type=operation_type,
            payload=payload,
            error_message=error_message,
        )
        entry.next_retry_at = _backoff_ts(0, self._base_backoff)
        entries.append(entry)
        self._save(entries)
        logger.info("error_queue: enqueued %s [%s]", entry.entry_id[:8], operation_type)
        return entry

    def list_entries(
        self,
        status: Optional[str] = None,
        include_resolved: bool = False,
    ) -> list[ErrorEntry]:
        """Return all (or filtered) queue entries.

        Args:
            status: If given, only return entries with this status string.
            include_resolved: If ``True``, include resolved entries.

        Returns:
            Filtered list of :class:`ErrorEntry`, newest first.
        """
        entries = self._load()
        if not include_resolved:
            entries = [e for e in entries if e.status != ErrorStatus.RESOLVED]
        if status:
            entries = [e for e in entries if e.status == status]
        return sorted(entries, key=lambda e: e.created_at, reverse=True)

    def due_entries(self) -> list[ErrorEntry]:
        """Return entries that are due for a retry attempt right now.

        Returns:
            List of :class:`ErrorEntry` objects whose ``next_retry_at`` is
            in the past and whose status is :attr:`ErrorStatus.PENDING`.
        """
        now = _now_iso()
        return [
            e
            for e in self._load()
            if e.status == ErrorStatus.PENDING
            and e.next_retry_at is not None
            and e.next_retry_at <= now
        ]

    def retry(
        self,
        entry_id: str,
        handler: Optional[Callable[[ErrorEntry], bool]] = None,
    ) -> bool:
        """Attempt to retry a single queued entry.

        The *handler* callable receives the :class:`ErrorEntry` and returns
        ``True`` on success, ``False`` on failure.  When no handler is
        provided the entry is simply promoted (useful for manual resolution).

        Args:
            entry_id: The ``entry_id`` of the entry to retry.
            handler: Optional callable ``(ErrorEntry) -> bool``.

        Returns:
            ``True`` if the retry succeeded (entry marked resolved),
            ``False`` otherwise.
        """
        entries = self._load()
        target = next((e for e in entries if e.entry_id == entry_id), None)
        if target is None:
            logger.warning("error_queue: retry — entry %s not found", entry_id)
            return False

        if target.status == ErrorStatus.EXHAUSTED:
            logger.info("error_queue: entry %s is exhausted; skipping", entry_id[:8])
            return False

        target.status = ErrorStatus.RETRYING
        self._save(entries)

        success = handler(target) if handler else False

        if success:
            target.status = ErrorStatus.RESOLVED
            logger.info("error_queue: entry %s resolved", entry_id[:8])
        else:
            target.retry_count += 1
            if target.retry_count >= self._max_retries:
                target.status = ErrorStatus.EXHAUSTED
                target.next_retry_at = None
                logger.warning(
                    "error_queue: entry %s exhausted after %d retries",
                    entry_id[:8],
                    target.retry_count,
                )
            else:
                target.status = ErrorStatus.PENDING
                target.next_retry_at = _backoff_ts(target.retry_count, self._base_backoff)
                logger.info(
                    "error_queue: entry %s rescheduled (attempt %d/%d)",
                    entry_id[:8],
                    target.retry_count,
                    self._max_retries,
                )

        self._save(entries)
        return success

    def retry_all_due(
        self,
        handler: Optional[Callable[[ErrorEntry], bool]] = None,
    ) -> dict[str, bool]:
        """Retry all entries that are currently due.

        Args:
            handler: Optional callable ``(ErrorEntry) -> bool``.

        Returns:
            Dict mapping ``entry_id`` to success bool.
        """
        due = self.due_entries()
        results: dict[str, bool] = {}
        for entry in due:
            results[entry.entry_id] = self.retry(entry.entry_id, handler=handler)
        return results

    def remove(self, entry_id: str) -> bool:
        """Permanently remove a single entry from the queue.

        Args:
            entry_id: The ``entry_id`` to remove.

        Returns:
            ``True`` if the entry was found and removed, ``False`` otherwise.
        """
        entries = self._load()
        before = len(entries)
        entries = [e for e in entries if e.entry_id != entry_id]
        if len(entries) == before:
            return False
        self._save(entries)
        return True

    def clear_all(self, status: Optional[str] = None) -> int:
        """Remove all entries (or all entries with a given status).

        Args:
            status: If given, only remove entries with this status string.

        Returns:
            Number of entries removed.
        """
        entries = self._load()
        if status:
            kept = [e for e in entries if e.status != status]
        else:
            kept = []
        removed = len(entries) - len(kept)
        self._save(kept)
        return removed

    def stats(self) -> dict[str, int]:
        """Return a count summary grouped by status.

        Returns:
            Dict mapping status string to count.
        """
        entries = self._load()
        counts: dict[str, int] = {s.value: 0 for s in ErrorStatus}
        for e in entries:
            counts[e.status] = counts.get(e.status, 0) + 1
        counts["total"] = len(entries)
        return counts


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _backoff_ts(attempt: int, base: int) -> str:
    """Compute next-retry timestamp using exponential backoff.

    delay = base * 2^attempt  (seconds)

    Args:
        attempt: Zero-based attempt index.
        base: Base delay in seconds.

    Returns:
        ISO-8601 UTC timestamp string.
    """
    import math
    from datetime import timedelta

    delay = base * (2 ** attempt)
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
