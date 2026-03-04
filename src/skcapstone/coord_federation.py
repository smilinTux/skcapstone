"""
SKCapstone Coordination Federation — Syncthing-based multi-instance task sync.

Watches ~/.skcapstone/coordination/ for incoming task and agent files from
peer instances. Handles last-writer-wins conflict resolution by mtime and
announces changes via the coord.sync pubsub topic.

Design:
    - Uses watchdog (inotify on Linux) to detect file-system events
    - Debounces events (Syncthing writes in stages)
    - Resolves Syncthing conflict files by mtime: newer wins
    - Publishes coord.sync messages so peers can react immediately

Syncthing conflict filename format:
    original-name.sync-conflict-YYYYMMDD-HHMMSS-DEVICEID.json
    → canonical name: original-name.json (conflict suffix stripped)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional

from .pubsub import PubSub

logger = logging.getLogger("skcapstone.coord_federation")

# Syncthing appends this pattern to the stem before the extension on conflict.
# e.g. "abc1-my-task.sync-conflict-20260302-120000-ABCDEF7.json"
_CONFLICT_RE = re.compile(r"\.sync-conflict-\d{8}-\d{6}-[A-Z0-9]+$", re.IGNORECASE)

# Pub/sub topic name for coordination sync events
COORD_SYNC_TOPIC = "coord.sync"


# ---------------------------------------------------------------------------
# Watchdog event handler
# ---------------------------------------------------------------------------


class _CoordEventHandler:
    """Watchdog FileSystemEventHandler for coordination directory.

    Filters to .json files only, debounces Syncthing multi-stage writes,
    and dispatches to a callback on create or modify.

    Args:
        callback: Called with the Path of each relevant changed file.
        debounce_ms: Minimum ms between events for the same file path.
    """

    def __init__(self, callback: Callable[[Path], None], debounce_ms: int = 500) -> None:
        self._callback = callback
        self._debounce_ms = debounce_ms
        self._last_event: dict[str, float] = {}

    def _accept(self, path_str: str) -> bool:
        """Return True if this event should be processed (not debounced)."""
        if not path_str.endswith(".json"):
            return False
        now = time.monotonic()
        last = self._last_event.get(path_str, 0.0)
        if (now - last) * 1000 < self._debounce_ms:
            return False
        self._last_event[path_str] = now
        # Prune stale entries every ~60 s
        cutoff = now - 60.0
        self._last_event = {k: v for k, v in self._last_event.items() if v > cutoff}
        return True

    def _dispatch(self, event) -> None:
        if getattr(event, "is_directory", False):
            return
        src = getattr(event, "src_path", str(event))
        if self._accept(src):
            self._callback(Path(src))

    def on_created(self, event) -> None:  # noqa: D401
        self._dispatch(event)

    def on_modified(self, event) -> None:  # noqa: D401
        self._dispatch(event)


# ---------------------------------------------------------------------------
# Federation watcher
# ---------------------------------------------------------------------------


class CoordFederationWatcher:
    """Watches the coordination directory for incoming peer files.

    Starts an inotify (watchdog) observer on:
        <shared_root>/coordination/tasks/
        <shared_root>/coordination/agents/

    On every new or modified .json file:
      1. If it is a Syncthing conflict file, resolve by mtime (newer wins).
      2. Publish a coord.sync pubsub message announcing the change.

    Thread safety: the watchdog observer runs in its own thread. File
    callbacks are dispatched back to the asyncio event loop via
    ``asyncio.run_coroutine_threadsafe``.

    Args:
        shared_root: Path to the shared ~/.skcapstone (or equivalent).
        agent_name: Name used as the pubsub sender.
        debounce_ms: Debounce window in milliseconds (default 500).
    """

    def __init__(
        self,
        shared_root: Path,
        agent_name: str = "anonymous",
        debounce_ms: int = 500,
    ) -> None:
        self._root = Path(shared_root).expanduser()
        self._coord_dir = self._root / "coordination"
        self._agent_name = agent_name
        self._debounce_ms = debounce_ms

        self._pubsub: Optional[PubSub] = None
        self._observer = None  # watchdog.Observer
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the inotify observer (call from the main asyncio thread).

        Args:
            loop: The running asyncio event loop (used for thread-safe dispatch).
        """
        try:
            from watchdog.observers import Observer
        except ImportError:
            logger.warning(
                "watchdog not installed — coord federation disabled. "
                "Install with: pip install watchdog"
            )
            return

        self._loop = loop
        self._pubsub = PubSub(self._root, agent_name=self._agent_name)
        self._pubsub.initialize()

        # Ensure the directories exist before watching
        (self._coord_dir / "tasks").mkdir(parents=True, exist_ok=True)
        (self._coord_dir / "agents").mkdir(parents=True, exist_ok=True)

        handler = _CoordEventHandler(self._on_fs_event, debounce_ms=self._debounce_ms)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._coord_dir), recursive=True)
        self._observer.start()
        logger.info("CoordFederationWatcher started on %s", self._coord_dir)

    def stop(self) -> None:
        """Stop the inotify observer."""
        if self._observer is not None:
            self._observer.stop()
            self._observer.join()
            self._observer = None
            logger.info("CoordFederationWatcher stopped")

    # ------------------------------------------------------------------
    # Event dispatch (watchdog thread → asyncio)
    # ------------------------------------------------------------------

    def _on_fs_event(self, path: Path) -> None:
        """Called from the watchdog thread. Forwards to the asyncio loop."""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._handle_change(path), self._loop)

    # ------------------------------------------------------------------
    # Core logic (runs in asyncio)
    # ------------------------------------------------------------------

    async def _handle_change(self, path: Path) -> None:
        """Process one incoming file change from a peer."""
        if not path.exists():
            return

        # Check if this is a Syncthing conflict file
        stem = path.stem  # e.g. "abc1-task.sync-conflict-20260302-120000-ABC"
        if _CONFLICT_RE.search(stem):
            await self._resolve_conflict(path)
        else:
            await self._announce(path, event="synced")

    async def _resolve_conflict(self, conflict_path: Path) -> None:
        """Resolve a Syncthing conflict file using last-writer-wins (mtime).

        The conflict file's stem contains the `.sync-conflict-DATE-TIME-ID`
        suffix. Strip it to find the canonical filename, then keep whichever
        version has the newer mtime and delete the loser.

        Args:
            conflict_path: Path to the `.sync-conflict-*.json` file.
        """
        stem = conflict_path.stem
        canonical_stem = _CONFLICT_RE.sub("", stem)
        canonical_path = conflict_path.parent / f"{canonical_stem}.json"

        if not canonical_path.exists():
            # No canonical version — promote conflict to canonical
            try:
                conflict_path.rename(canonical_path)
                logger.info("Promoted conflict to canonical: %s", canonical_path.name)
                await self._announce(canonical_path, event="conflict_resolved")
            except OSError as exc:
                logger.warning("Could not promote conflict file: %s", exc)
            return

        # Compare modification times
        try:
            conflict_mtime = conflict_path.stat().st_mtime
            canonical_mtime = canonical_path.stat().st_mtime
        except OSError:
            return

        if conflict_mtime > canonical_mtime:
            # Conflict is newer — replace canonical
            try:
                conflict_path.replace(canonical_path)
                logger.info(
                    "Conflict newer — replaced canonical: %s", canonical_path.name
                )
                await self._announce(canonical_path, event="conflict_resolved")
            except OSError as exc:
                logger.warning("Could not replace canonical with conflict: %s", exc)
        else:
            # Canonical is newer (or same age) — drop conflict
            try:
                conflict_path.unlink(missing_ok=True)
                logger.debug("Dropped older conflict: %s", conflict_path.name)
            except OSError as exc:
                logger.warning("Could not remove conflict file: %s", exc)

    async def _announce(self, path: Path, event: str = "synced") -> None:
        """Publish a coord.sync message for a changed coordination file.

        Payload schema:
            event       — "synced" | "conflict_resolved"
            kind        — "tasks" | "agents" | "other"
            file        — filename (not full path)
            task_id     — (tasks only) task id from JSON
            title       — (tasks only) task title
            agent       — (agents only) agent name from JSON
            source      — announcing agent name

        Args:
            path: The canonical file that changed.
            event: Human-readable event type.
        """
        if self._pubsub is None:
            return

        # Determine sub-directory kind
        try:
            rel = path.relative_to(self._coord_dir)
        except ValueError:
            return
        kind = rel.parts[0] if rel.parts else "other"  # "tasks" or "agents"

        # Read file content for metadata
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}

        payload: dict = {
            "event": event,
            "kind": kind,
            "file": path.name,
            "source": self._agent_name,
        }
        if kind == "tasks":
            payload["task_id"] = data.get("id")
            payload["title"] = data.get("title")
            payload["priority"] = data.get("priority")
        elif kind == "agents":
            payload["agent"] = data.get("agent")
            payload["state"] = data.get("state")

        try:
            self._pubsub.publish(COORD_SYNC_TOPIC, payload, ttl_seconds=3600)
            logger.info("coord.sync: %s %s/%s", event, kind, path.name)
        except Exception as exc:
            logger.warning("Failed to publish coord.sync: %s", exc)
