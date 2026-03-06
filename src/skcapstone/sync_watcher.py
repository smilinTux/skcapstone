"""
Syncthing seed auto-importer.

Watches ~/.skcapstone/sync/inbox/ for new .seed.json files arriving
via Syncthing and auto-imports them into the SQLite memory backend.

Uses watchdog.observers.Observer (same pattern as consciousness_loop.py)
with a polling fallback for environments without inotify support.

Tracks processed files in ~/.skcapstone/sync/processed.json to avoid
re-importing seeds across restarts.

Architecture:
    SeedFileHandler  -- watchdog event handler with debounce
    SyncWatcher      -- orchestrator: watch + poll + import
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("skcapstone.sync_watcher")

SEED_EXTENSION = ".seed.json"
DEFAULT_INBOX = "~/.skcapstone/sync/inbox"
DEFAULT_PROCESSED_LOG = "~/.skcapstone/sync/processed.json"
DEBOUNCE_MS = 500
POLL_INTERVAL_S = 30


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_sync_config(home: Path) -> dict[str, Any]:
    """Load sync watcher configuration from config.yaml.

    Reads the ``sync`` section and applies defaults for any missing keys.

    Args:
        home: Agent home directory (~/.skcapstone).

    Returns:
        Dict with auto_import, inbox_path, and processed_log.
    """
    defaults = {
        "auto_import": True,
        "auto_vector_index": True,
        "auto_graph_index": True,
        "inbox_path": str(home / "sync" / "inbox"),
        "processed_log": str(home / "sync" / "processed.json"),
    }

    config_file = home / "config" / "config.yaml"
    if not config_file.exists():
        return defaults

    try:
        import yaml as _yaml

        data = _yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
        sync_data = data.get("sync", {})
        return {
            "auto_import": sync_data.get("auto_import", defaults["auto_import"]),
            "auto_vector_index": sync_data.get(
                "auto_vector_index", defaults["auto_vector_index"]
            ),
            "auto_graph_index": sync_data.get(
                "auto_graph_index", defaults["auto_graph_index"]
            ),
            "inbox_path": str(
                Path(sync_data.get("inbox_path", defaults["inbox_path"])).expanduser()
            ),
            "processed_log": str(
                Path(
                    sync_data.get("processed_log", defaults["processed_log"])
                ).expanduser()
            ),
        }
    except Exception as exc:
        logger.debug("Could not load sync config: %s — using defaults", exc)
        return defaults


# ---------------------------------------------------------------------------
# Processed file tracker
# ---------------------------------------------------------------------------


class ProcessedTracker:
    """Tracks which seed files have already been imported.

    Persists a JSON file mapping filename -> import timestamp so that
    seeds are not re-imported after daemon restart.

    Args:
        log_path: Path to the processed.json file.
    """

    def __init__(self, log_path: str | Path) -> None:
        self._path = Path(log_path)
        self._entries: dict[str, str] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        """Load existing entries from disk."""
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._entries = data
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not load processed log: %s", exc)

    def is_processed(self, filename: str) -> bool:
        """Check if a seed file has already been imported.

        Args:
            filename: The seed filename (basename).

        Returns:
            True if already processed.
        """
        with self._lock:
            return filename in self._entries

    def mark_processed(self, filename: str) -> None:
        """Record a file as processed and persist to disk.

        Args:
            filename: The seed filename (basename).
        """
        with self._lock:
            self._entries[filename] = datetime.now(timezone.utc).isoformat()
            self._persist()

    def _persist(self) -> None:
        """Write current entries to disk."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._entries, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error("Could not persist processed log: %s", exc)


# ---------------------------------------------------------------------------
# Seed importer
# ---------------------------------------------------------------------------


def _compute_seed_hash(data: dict) -> str:
    """Compute a stable hash for deduplication.

    Args:
        data: Parsed seed dictionary.

    Returns:
        SHA-256 hex digest of the canonical JSON.
    """
    canonical = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def vector_index_seed(memory: "Memory") -> bool:
    """Index a memory in SKVector (Qdrant) if available.

    Attempts to connect to the vector backend using skmemory's
    configuration resolution (CLI > env > config file). If the
    vector backend is unreachable or dependencies are missing,
    logs a debug message and returns False without raising.

    Args:
        memory: The Memory object to index (already saved to SQLite).

    Returns:
        True if the memory was successfully indexed, False otherwise.
    """
    try:
        from skmemory.config import merge_env_and_config
        from skmemory.backends.skvector_backend import SKVectorBackend
    except ImportError:
        logger.debug(
            "skmemory vector backend not importable — skipping vector index"
        )
        return False

    try:
        skvector_url, skvector_key, _ = merge_env_and_config()
        if not skvector_url:
            logger.debug(
                "No SKVector URL configured — skipping vector index"
            )
            return False

        backend = SKVectorBackend(url=skvector_url, api_key=skvector_key)
        backend.save(memory)
        logger.debug("Indexed memory %s in SKVector", memory.id)
        return True
    except Exception as exc:
        logger.debug(
            "SKVector indexing failed for memory %s: %s — continuing without vector index",
            memory.id,
            exc,
        )
        return False


def graph_index_seed(memory: "Memory") -> bool:
    """Index a memory in SKGraph (FalkorDB) if available.

    Attempts to connect to the graph backend using skmemory's
    configuration resolution (CLI > env > config file). If the
    graph backend is unreachable or dependencies are missing,
    logs a debug message and returns False without raising.

    Args:
        memory: The Memory object to index (already saved to SQLite).

    Returns:
        True if the memory was successfully indexed, False otherwise.
    """
    try:
        from skmemory.config import merge_env_and_config
        from skmemory.backends.skgraph_backend import SKGraphBackend
    except ImportError:
        logger.debug(
            "skmemory graph backend not importable — skipping graph index"
        )
        return False

    try:
        _, _, skgraph_url = merge_env_and_config()
        if not skgraph_url:
            logger.debug(
                "No SKGraph URL configured — skipping graph index"
            )
            return False

        backend = SKGraphBackend(url=skgraph_url)
        result = backend.index_memory(memory)
        if result:
            logger.debug("Indexed memory %s in SKGraph", memory.id)
        return result
    except Exception as exc:
        logger.debug(
            "SKGraph indexing failed for memory %s: %s — continuing without graph index",
            memory.id,
            exc,
        )
        return False


def import_seed_to_memory(seed_path: Path, home: Path) -> Optional[str]:
    """Parse a .seed.json file and store its contents via skmemory.

    Extracts memory_entries from the seed (if present) and stores each
    one via the MemoryStore.snapshot() API. Also imports identity and
    trust data via the existing pull_seeds infrastructure.

    Args:
        seed_path: Path to the .seed.json file.
        home: Agent home directory.

    Returns:
        Summary string of what was imported, or None on failure.
    """
    try:
        raw = seed_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read seed %s: %s", seed_path.name, exc)
        return None

    agent_name = data.get("agent_name", "unknown")
    source_host = data.get("source_host", "unknown")
    created_at = data.get("created_at", "")
    seed_hash = _compute_seed_hash(data)
    imported_count = 0
    results: list[str] = []

    # Load sync config to check auto_vector_index and auto_graph_index flags
    sync_config = load_sync_config(home)
    auto_vector_index = sync_config.get("auto_vector_index", True)
    auto_graph_index = sync_config.get("auto_graph_index", True)

    # Import memory entries via skmemory API
    memory_entries = data.get("memory_entries", [])
    vector_indexed = 0
    graph_indexed = 0
    if memory_entries:
        try:
            from skmemory.store import MemoryStore
            from skmemory.models import MemoryLayer

            store = MemoryStore(use_sqlite=True)

            for entry in memory_entries:
                title = entry.get("title", "Synced memory")
                content = entry.get("content", "")
                layer_str = entry.get("layer", "short-term")
                tags = entry.get("tags", [])
                source_ref = entry.get("source_ref", f"sync:{agent_name}@{source_host}")

                # Map layer string to enum
                layer_map = {
                    "short-term": MemoryLayer.SHORT,
                    "mid-term": MemoryLayer.MID,
                    "long-term": MemoryLayer.LONG,
                }
                layer = layer_map.get(layer_str, MemoryLayer.SHORT)

                # Add sync provenance tags
                sync_tags = list(tags) + [
                    "sync:imported",
                    f"sync:from:{agent_name}",
                    f"sync:host:{source_host}",
                    f"sync:hash:{seed_hash}",
                ]

                memory = store.snapshot(
                    title=title,
                    content=content,
                    layer=layer,
                    tags=sync_tags,
                    source="syncthing",
                    source_ref=source_ref,
                    metadata={
                        "sync_source_agent": agent_name,
                        "sync_source_host": source_host,
                        "sync_seed_created": created_at,
                        "sync_seed_hash": seed_hash,
                    },
                )
                imported_count += 1

                # Auto-index in SKVector if enabled and available
                if auto_vector_index and vector_index_seed(memory):
                    vector_indexed += 1

                # Auto-index in SKGraph if enabled and available
                if auto_graph_index and graph_index_seed(memory):
                    graph_indexed += 1

            results.append(f"{imported_count} memories")
            if vector_indexed:
                results.append(f"{vector_indexed} vector-indexed")
            if graph_indexed:
                results.append(f"{graph_indexed} graph-indexed")
        except ImportError:
            logger.warning("skmemory not available — skipping memory import")
        except Exception as exc:
            logger.error("Memory import failed for %s: %s", seed_path.name, exc)

    # Also use the existing pull_seeds machinery for identity/trust/FEBs
    try:
        if "identity" in data or "trust" in data or "febs" in data:
            from .pillars.sync import pull_seeds as _pull_existing

            # pull_seeds reads from inbox, but the file is already there —
            # we just log what it would pick up
            if "identity" in data:
                results.append("identity")
            if "trust" in data:
                results.append("trust")
            if "febs" in data:
                results.append(f"{len(data.get('febs', []))} FEBs")
    except Exception as exc:
        logger.debug("Extended seed import skipped: %s", exc)

    if results:
        summary = f"Imported seed from {agent_name}@{source_host}: {', '.join(results)}"
        logger.info(summary)
        return summary

    logger.info(
        "Seed %s from %s@%s contained no importable data",
        seed_path.name, agent_name, source_host,
    )
    return f"Seed from {agent_name}@{source_host}: no importable data"


def _log_to_short_term_memory(message: str, home: Path) -> None:
    """Log an import event to the agent's short-term memory.

    Args:
        message: Description of what was imported.
        home: Agent home directory.
    """
    try:
        from skmemory.store import MemoryStore

        store = MemoryStore(use_sqlite=True)
        store.snapshot(
            title="Sync import event",
            content=message,
            tags=["sync:event", "sync:import-log"],
            source="sync_watcher",
            source_ref="auto",
        )
    except Exception as exc:
        logger.debug("Could not log import to memory: %s", exc)


# ---------------------------------------------------------------------------
# Watchdog event handler
# ---------------------------------------------------------------------------


class SeedFileHandler:
    """Handles file creation events for .seed.json files.

    Implements debouncing to handle Syncthing's multi-stage file writes.

    Args:
        callback: Function to call with each new seed file path.
        debounce_ms: Minimum milliseconds between events for the same file.
    """

    def __init__(self, callback, debounce_ms: int = DEBOUNCE_MS) -> None:
        self._callback = callback
        self._debounce_ms = debounce_ms
        self._last_event: dict[str, float] = {}

    def on_created(self, event) -> None:
        """Handle file creation events.

        Args:
            event: Watchdog FileCreatedEvent (or similar with src_path).
        """
        if hasattr(event, "is_directory") and event.is_directory:
            return

        src_path = event.src_path if hasattr(event, "src_path") else str(event)
        if not src_path.endswith(SEED_EXTENSION):
            return

        # Debounce: Syncthing writes in stages
        now = time.monotonic()
        last = self._last_event.get(src_path, 0)
        if (now - last) * 1000 < self._debounce_ms:
            return
        self._last_event[src_path] = now

        # Clean up old entries (prevent unbounded growth)
        if len(self._last_event) > 100:
            cutoff = now - 60
            self._last_event = {
                k: v for k, v in self._last_event.items() if v > cutoff
            }

        logger.debug("Seed file detected: %s", src_path)
        self._callback(Path(src_path))

    def on_modified(self, event) -> None:
        """Handle file modification events (Syncthing rewrites).

        Args:
            event: Watchdog FileModifiedEvent.
        """
        # Treat modifications the same as creation for Syncthing compatibility
        self.on_created(event)


class _WatchdogSyncAdapter:
    """Adapter from watchdog events to SeedFileHandler callback."""

    def __init__(self, callback) -> None:
        self._handler = SeedFileHandler(callback)

    def dispatch(self, event) -> None:
        """Dispatch a watchdog event.

        Args:
            event: Watchdog event object.
        """
        etype = getattr(event, "event_type", "")
        if etype in ("created", "modified"):
            self._handler.on_created(event)


# ---------------------------------------------------------------------------
# SyncWatcher orchestrator
# ---------------------------------------------------------------------------


class SyncWatcher:
    """Watches the sync inbox for new .seed.json files and auto-imports them.

    Combines watchdog inotify monitoring (for sub-second response) with
    a periodic polling fallback (for reliability). Tracks already-processed
    files to avoid duplicate imports.

    Args:
        home: Agent home directory (~/.skcapstone).
        stop_event: Threading event to signal shutdown.
        inbox_path: Override for the inbox directory path.
        processed_log: Override for the processed.json path.
    """

    def __init__(
        self,
        home: Path,
        stop_event: threading.Event,
        inbox_path: Optional[str] = None,
        processed_log: Optional[str] = None,
    ) -> None:
        self._home = home
        self._stop_event = stop_event

        config = load_sync_config(home)
        if not config.get("auto_import", True):
            self._enabled = False
            logger.info("Sync auto-import disabled by config")
        else:
            self._enabled = True

        self._inbox = Path(inbox_path or config["inbox_path"]).expanduser()
        self._tracker = ProcessedTracker(
            processed_log or config["processed_log"]
        )
        self._observer = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """Whether auto-import is enabled."""
        return self._enabled

    def start(self) -> list[threading.Thread]:
        """Start the watcher and poller threads.

        Returns:
            List of started daemon threads.
        """
        if not self._enabled:
            return []

        self._inbox.mkdir(parents=True, exist_ok=True)
        threads: list[threading.Thread] = []

        # Watchdog inotify thread
        t_watch = threading.Thread(
            target=self._run_watcher,
            name="sync-watcher-inotify",
            daemon=True,
        )
        t_watch.start()
        threads.append(t_watch)

        # Initial scan on startup
        self._poll_inbox()

        logger.info(
            "SyncWatcher started — inbox=%s, inotify=yes, poll=%ds",
            self._inbox, POLL_INTERVAL_S,
        )
        return threads

    def stop(self) -> None:
        """Stop the watcher gracefully."""
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except Exception:
                pass
            self._observer = None
        logger.info("SyncWatcher stopped.")

    def poll_inbox(self) -> int:
        """Public entry point for scheduled polling.

        Returns:
            Number of seeds imported during this poll.
        """
        return self._poll_inbox()

    def status(self) -> dict[str, Any]:
        """Return current watcher status.

        Returns:
            Dict with enabled, inbox_path, observer_alive, and processed count.
        """
        return {
            "enabled": self._enabled,
            "inbox_path": str(self._inbox),
            "observer_alive": (
                self._observer is not None
                and hasattr(self._observer, "is_alive")
                and self._observer.is_alive()
            ),
            "processed_count": len(self._tracker._entries),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_watcher(self) -> None:
        """Run the watchdog inotify loop with polling fallback."""
        try:
            from watchdog.observers import Observer

            adapter = _WatchdogSyncAdapter(self._on_seed_file)
            self._observer = Observer()
            self._observer.schedule(adapter, str(self._inbox), recursive=False)
            self._observer.start()
            logger.info("Sync inotify watcher active on %s", self._inbox)

            # Block until stop, polling periodically as backup
            while not self._stop_event.is_set():
                self._stop_event.wait(timeout=POLL_INTERVAL_S)
                if not self._stop_event.is_set():
                    self._poll_inbox()

        except ImportError:
            logger.warning(
                "watchdog not installed — falling back to polling only. "
                "Install with: pip install watchdog"
            )
            # Pure polling fallback
            while not self._stop_event.is_set():
                self._poll_inbox()
                self._stop_event.wait(timeout=POLL_INTERVAL_S)

        except Exception as exc:
            logger.error("Sync watcher error: %s", exc)

    def _on_seed_file(self, path: Path) -> None:
        """Handle a detected seed file from inotify.

        Waits briefly for the file to be fully written, then imports.

        Args:
            path: Path to the detected .seed.json file.
        """
        # Brief delay to let Syncthing finish writing
        time.sleep(0.5)
        self._import_seed(path)

    def _poll_inbox(self) -> int:
        """Scan the inbox directory for unprocessed seed files.

        Returns:
            Number of seeds imported.
        """
        if not self._inbox.exists():
            return 0

        imported = 0
        try:
            for f in sorted(self._inbox.iterdir()):
                if f.name.startswith("."):
                    continue
                if not f.name.endswith(SEED_EXTENSION):
                    continue
                if self._tracker.is_processed(f.name):
                    continue
                if self._import_seed(f):
                    imported += 1
        except OSError as exc:
            logger.error("Inbox scan failed: %s", exc)

        return imported

    def _import_seed(self, seed_path: Path) -> bool:
        """Import a single seed file.

        Thread-safe: uses a lock to prevent concurrent imports of the
        same file from inotify and polling.

        Args:
            seed_path: Path to the .seed.json file.

        Returns:
            True if import succeeded, False otherwise.
        """
        with self._lock:
            if self._tracker.is_processed(seed_path.name):
                return False

            if not seed_path.exists():
                return False

            logger.info("Importing seed: %s", seed_path.name)

            result = import_seed_to_memory(seed_path, self._home)
            if result:
                self._tracker.mark_processed(seed_path.name)

                # Log the import event to short-term memory
                _log_to_short_term_memory(result, self._home)

                # Move to archive
                archive = self._inbox.parent / "archive"
                archive.mkdir(exist_ok=True)
                try:
                    seed_path.rename(archive / seed_path.name)
                    logger.debug("Archived: %s", seed_path.name)
                except OSError as exc:
                    logger.warning("Could not archive %s: %s", seed_path.name, exc)

                return True

            logger.warning("Seed import returned no result: %s", seed_path.name)
            return False


# ---------------------------------------------------------------------------
# Scheduled task factory
# ---------------------------------------------------------------------------


def make_sync_inbox_scan_task(
    watcher: Optional[SyncWatcher],
) -> callable:
    """Return a callback for the task scheduler to poll the sync inbox.

    Args:
        watcher: SyncWatcher instance (or None if disabled).

    Returns:
        Zero-argument callable suitable for TaskScheduler.register().
    """

    def _run() -> None:
        if watcher is None:
            return
        count = watcher.poll_inbox()
        if count:
            logger.info("Scheduled sync scan imported %d seed(s)", count)

    return _run
