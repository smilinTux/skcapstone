"""Fallback event tracker — graceful degradation logging.

Records every LLM fallback event to ~/.skcapstone/fallbacks.json so
operators can diagnose which backends are failing and how often the
agent is degrading to lower-quality providers.

Architecture:
    FallbackEvent  — Pydantic model for a single fallback occurrence
    FallbackTracker — thread-safe writer / reader for fallbacks.json
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from . import AGENT_HOME

logger = logging.getLogger("skcapstone.fallback_tracker")

_DEFAULT_PATH = Path(AGENT_HOME).expanduser() / "fallbacks.json"
_MAX_EVENTS = 1000  # cap file size; rotate oldest when exceeded


class FallbackEvent(BaseModel):
    """A single LLM fallback occurrence.

    Attributes:
        timestamp: ISO-8601 UTC timestamp of the event.
        primary_model: The model that was originally selected.
        primary_backend: The backend provider of the primary model.
        fallback_model: The model actually used (or ``"none"`` if all failed).
        fallback_backend: The backend that served the response.
        reason: Short human-readable description of why the fallback occurred.
        success: Whether the fallback itself produced a usable response.
    """

    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    primary_model: str
    primary_backend: str
    fallback_model: str
    fallback_backend: str
    reason: str
    success: bool


class FallbackTracker:
    """Thread-safe store for fallback events.

    Events are appended to a JSON file (list of objects). The file is
    created on first write. Reads never raise — a missing or corrupt
    file returns an empty list.

    Args:
        path: Path to the fallbacks JSON file.
               Defaults to ``~/.skcapstone/fallbacks.json``.
        max_events: Maximum number of events retained (oldest are pruned).
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        max_events: int = _MAX_EVENTS,
    ) -> None:
        self._path = Path(path) if path is not None else _DEFAULT_PATH
        self._max_events = max_events
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, event: FallbackEvent) -> None:
        """Append *event* to the fallback log.

        Args:
            event: The fallback event to persist.
        """
        with self._lock:
            events = self._load_raw()
            events.append(event.model_dump())
            if len(events) > self._max_events:
                events = events[-self._max_events :]
            self._save_raw(events)
        logger.debug(
            "Fallback recorded: %s → %s (%s, success=%s)",
            event.primary_backend,
            event.fallback_backend,
            event.reason,
            event.success,
        )

    def load_events(self, limit: int = 0) -> list[FallbackEvent]:
        """Return stored fallback events, newest first.

        Args:
            limit: If > 0, return only the *limit* most recent events.

        Returns:
            List of :class:`FallbackEvent` objects.
        """
        with self._lock:
            raw = self._load_raw()

        events: list[FallbackEvent] = []
        for item in reversed(raw):
            try:
                events.append(FallbackEvent(**item))
            except Exception:  # noqa: BLE001
                continue  # skip corrupt entries

        if limit > 0:
            return events[:limit]
        return events

    def clear(self) -> int:
        """Delete all stored fallback events.

        Returns:
            Number of events that were cleared.
        """
        with self._lock:
            raw = self._load_raw()
            count = len(raw)
            self._save_raw([])
        return count

    @property
    def path(self) -> Path:
        """Path to the fallbacks JSON file."""
        return self._path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_raw(self) -> list[dict]:
        """Load raw JSON list from disk without locking."""
        if not self._path.exists():
            return []
        try:
            text = self._path.read_text(encoding="utf-8")
            data = json.loads(text)
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError):
            logger.warning("fallbacks.json is corrupt or unreadable — resetting")
        return []

    def _save_raw(self, events: list[dict]) -> None:
        """Write raw JSON list to disk without locking."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(events, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# Module-level singleton — shared across the process
_tracker: Optional[FallbackTracker] = None
_tracker_lock = threading.Lock()


def get_tracker(path: Optional[Path] = None) -> FallbackTracker:
    """Return the module-level :class:`FallbackTracker` singleton.

    Creates it on first call. Passing *path* on the first call
    customises the storage location.

    Args:
        path: Optional override for the fallbacks file path.

    Returns:
        The singleton :class:`FallbackTracker`.
    """
    global _tracker
    with _tracker_lock:
        if _tracker is None:
            _tracker = FallbackTracker(path=path)
    return _tracker
