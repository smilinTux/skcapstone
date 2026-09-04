"""Fallback event tracker - graceful degradation logging.

Records every LLM fallback event so operators can diagnose which backends are
failing and how often the agent is degrading to lower-quality providers.

Storage is one append-only JSONL file per writer::

    ~/.skcapstone/fallbacks/<agent>@<node>.jsonl

``~/.skcapstone`` is Syncthing-replicated. The previous layout was a single
``fallbacks.json`` holding a JSON list that every writer re-read, mutated and
rewrote in full - a read-modify-write on a shared path, which is exactly the
pattern behind prb-7810b08e. ``threading.Lock`` orders writers inside one
process and does nothing across processes or nodes, so copies diverged and
Syncthing produced conflicts that silently dropped events.

One file per (agent, node) makes the write sets disjoint, so replication has
nothing to conflict on, and appending a line never rewrites existing rows.
Reads fold every writer file; only the owning process ever trims its own.

Architecture:
    FallbackEvent  - Pydantic model for a single fallback occurrence
    FallbackTracker - per-writer append-only writer / fleet-wide reader
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from . import AGENT_HOME, active_agent_name

logger = logging.getLogger("skcapstone.fallback_tracker")

_DEFAULT_ROOT = Path(AGENT_HOME).expanduser() / "fallbacks"
_LEGACY_PATH = Path(AGENT_HOME).expanduser() / "fallbacks.json"
_LEGACY_WRITER = "_legacy"
_MAX_EVENTS = 1000  # cap per-writer file size; oldest rows are dropped
_UNSAFE = re.compile(r"[^A-Za-z0-9._+-]")


def _segment(value: str | None, default: str) -> str:
    """Normalize one identity component into a safe filename segment."""
    raw = (value or "").strip() or default
    cleaned = _UNSAFE.sub("-", raw)[:100]
    return cleaned or default


def writer_name(agent: str | None = None, node: str | None = None) -> str:
    """Return the ``<agent>@<node>`` writer identity for this process.

    Mirrors the ITIL/activity convention: a bare ``<agent>`` is forbidden,
    because two nodes running the same agent would then share a file.
    """
    return (
        f"{_segment(agent or active_agent_name(), 'unknown-agent')}"
        f"@{_segment(node or socket.gethostname(), 'unknown-node')}"
    )


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
    """Append-only, per-writer store for fallback events.

    Each ``(agent, node)`` pair owns exactly one JSONL file under *root* and
    only ever appends to it, so replicating the directory can never produce a
    conflict. Reads fold every writer file in the directory. Reads never raise -
    a missing directory or an unreadable line yields no events.

    Args:
        root: Directory holding the writer files. Defaults to
            ``~/.skcapstone/fallbacks``.
        agent: Writer's agent name (defaults to the active agent).
        node: Writer's node name (defaults to this host).
        max_events: Rows retained in *this writer's own* file; oldest dropped.
    """

    def __init__(
        self,
        root: Optional[Path] = None,
        agent: Optional[str] = None,
        node: Optional[str] = None,
        max_events: int = _MAX_EVENTS,
    ) -> None:
        self._root = Path(root) if root is not None else _DEFAULT_ROOT
        self._writer = writer_name(agent, node)
        self._max_events = max_events
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, event: FallbackEvent) -> None:
        """Append *event* to this writer's own log.

        Args:
            event: The fallback event to persist.
        """
        line = json.dumps(event.model_dump(), ensure_ascii=False) + "\n"
        with self._lock:
            self._root.mkdir(parents=True, exist_ok=True)
            # O_APPEND keeps concurrent writers on this node from interleaving
            # partial rows; the flock additionally orders append against trim.
            fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
            self._trim_own()
        logger.debug(
            "Fallback recorded: %s → %s (%s, success=%s)",
            event.primary_backend,
            event.fallback_backend,
            event.reason,
            event.success,
        )

    def load_events(self, limit: int = 0) -> list[FallbackEvent]:
        """Return stored fallback events from every writer, newest first.

        Args:
            limit: If > 0, return only the *limit* most recent events.

        Returns:
            List of :class:`FallbackEvent` objects ordered by timestamp desc.
        """
        rows: list[dict] = []
        for path in self._writer_files():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue  # skip a torn or corrupt row
                if isinstance(item, dict):
                    rows.append(item)

        rows.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
        events: list[FallbackEvent] = []
        for item in rows:
            try:
                events.append(FallbackEvent(**item))
            except Exception:  # noqa: BLE001
                continue  # skip corrupt entries
            if limit > 0 and len(events) >= limit:
                break
        return events

    def clear(self) -> int:
        """Delete every stored fallback event.

        Unlike :meth:`record`, this removes all writer files, not just this
        one's. Deleting a replicated file converges cleanly, and clearing is a
        rare operator action rather than the hot path that caused conflicts.

        Returns:
            Number of events that were cleared.
        """
        with self._lock:
            count = 0
            for path in self._writer_files():
                try:
                    count += sum(
                        1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
                    )
                    path.unlink()
                except OSError:
                    continue
        return count

    @property
    def path(self) -> Path:
        """This writer's own append-only file."""
        return self._root / f"{self._writer}.jsonl"

    @property
    def root(self) -> Path:
        """Directory holding every writer's file."""
        return self._root

    @property
    def writer(self) -> str:
        """This writer's ``<agent>@<node>`` identity."""
        return self._writer

    def writers(self) -> list[str]:
        """Return every writer identity present in the directory."""
        return sorted(path.stem for path in self._writer_files())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _writer_files(self) -> list[Path]:
        if not self._root.is_dir():
            return []
        return sorted(self._root.glob("*.jsonl"))

    def _trim_own(self) -> None:
        """Drop the oldest rows from this writer's file only."""
        path = self.path
        try:
            lines = [
                line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
            ]
        except OSError:
            return
        if len(lines) <= self._max_events:
            return
        keep = lines[-self._max_events :]
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
        os.replace(tmp, path)


def migrate_legacy_fallbacks(
    legacy: Optional[Path] = None,
    root: Optional[Path] = None,
) -> int:
    """Convert a pre-split ``fallbacks.json`` list into a ``_legacy`` writer.

    The old file was written by every node, so no node may claim it; its rows
    move to a writer nobody appends to but everybody reads.

    Args:
        legacy: The old list file (defaults to ``~/.skcapstone/fallbacks.json``).
        root: Target writer directory (defaults to ``~/.skcapstone/fallbacks``).

    Returns:
        Number of rows migrated (0 when there is nothing to do).
    """
    legacy_path = Path(legacy) if legacy is not None else _LEGACY_PATH
    target_root = Path(root) if root is not None else _DEFAULT_ROOT
    if not legacy_path.exists():
        return 0
    try:
        data = json.loads(legacy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = []
    rows = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    target_root.mkdir(parents=True, exist_ok=True)
    out = target_root / f"{_LEGACY_WRITER}.jsonl"
    with out.open("a", encoding="utf-8") as stream:
        for item in rows:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")
    legacy_path.unlink()
    logger.info("Migrated %d legacy fallback row(s) into %s", len(rows), out)
    return len(rows)


# Module-level singleton - shared across the process
_tracker: Optional[FallbackTracker] = None
_tracker_lock = threading.Lock()


def get_tracker(root: Optional[Path] = None) -> FallbackTracker:
    """Return the module-level :class:`FallbackTracker` singleton.

    Creates it on first call. Passing *root* on the first call customises the
    writer directory.

    Args:
        root: Optional override for the fallbacks writer directory.

    Returns:
        The singleton :class:`FallbackTracker`.
    """
    global _tracker
    with _tracker_lock:
        if _tracker is None:
            _tracker = FallbackTracker(root=root)
    return _tracker
