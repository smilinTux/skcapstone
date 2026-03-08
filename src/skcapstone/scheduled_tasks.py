"""
Cron-like scheduler for recurring agent background tasks.

Runs a single daemon thread that wakes up every TICK_INTERVAL seconds,
checks which tasks are due, and fires their callbacks.

Built-in recurring tasks:
    - heartbeat_pulse        — every 30 seconds
    - backend_reprobe        — every 5 minutes
    - memory_promotion_sweep — every hour
    - profile_freshness_check — every 24 hours
    - dreaming_reflection    — every 15 minutes

Usage:
    scheduler = build_scheduler(home, stop_event, consciousness_loop, beacon)
    thread = scheduler.start()          # returns the daemon thread
    print(scheduler.status())           # list[dict] of task state
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("skcapstone.scheduled_tasks")


# ---------------------------------------------------------------------------
# Core data structures
# ---------------------------------------------------------------------------


@dataclass
class ScheduledTask:
    """A recurring task entry managed by TaskScheduler.

    Attributes:
        name: Unique task name.
        interval_seconds: How often (in seconds) the task should run.
        callback: Zero-argument callable invoked on each run.
        last_run: UTC timestamp of the most recent execution (or None if never run).
        last_error: String representation of the last exception, if any.
        run_count: Total number of successful (non-raising) executions.
        error_count: Total number of executions that raised an exception.
    """

    name: str
    interval_seconds: float
    callback: Callable[[], None]
    last_run: Optional[datetime] = None
    last_error: Optional[str] = None
    run_count: int = 0
    error_count: int = 0

    def is_due(self, now: Optional[datetime] = None) -> bool:
        """Return True if the task interval has elapsed since last_run.

        A task with no prior run is always considered due.

        Args:
            now: Reference time for the check (defaults to UTC now).
        """
        if self.last_run is None:
            return True
        reference = now or datetime.now(timezone.utc)
        elapsed = (reference - self.last_run).total_seconds()
        return elapsed >= self.interval_seconds

    def run(self) -> None:
        """Execute the callback, recording outcome regardless of success.

        On success: increments run_count, clears last_error.
        On failure: increments error_count, stores exception string in last_error.
        In both cases last_run is updated so the interval resets.
        """
        try:
            self.callback()
            self.run_count += 1
            self.last_error = None
            logger.debug("Scheduled task '%s' completed (run #%d)", self.name, self.run_count)
        except Exception as exc:
            self.error_count += 1
            self.last_error = str(exc)
            logger.error("Scheduled task '%s' failed: %s", self.name, exc)
        finally:
            self.last_run = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class TaskScheduler:
    """Cron-like scheduler that fires registered tasks on configurable intervals.

    Runs one daemon thread (``daemon-scheduler``) that wakes up every
    ``TICK_INTERVAL`` seconds, checks which tasks are due, and calls their
    callbacks inline (serially, in registration order).

    Args:
        home: Agent home directory.
        stop_event: Daemon stop event; scheduler thread exits when set.
        tick_interval: How often (seconds) the scheduler loop wakes to check tasks.
    """

    TICK_INTERVAL: float = 5.0

    def __init__(
        self,
        home: Path,
        stop_event: threading.Event,
        tick_interval: float = TICK_INTERVAL,
    ) -> None:
        self._home = home
        self._stop_event = stop_event
        self._tick_interval = tick_interval
        self._tasks: list[ScheduledTask] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        interval_seconds: float,
        callback: Callable[[], None],
    ) -> ScheduledTask:
        """Register a recurring task.

        Args:
            name: Unique task name (used in logs and status output).
            interval_seconds: Minimum seconds between executions.
            callback: Zero-argument callable to invoke.

        Returns:
            The created ScheduledTask (caller may inspect it at runtime).
        """
        task = ScheduledTask(name=name, interval_seconds=interval_seconds, callback=callback)
        with self._lock:
            self._tasks.append(task)
        logger.debug("Registered scheduled task '%s' every %.0fs", name, interval_seconds)
        return task

    def start(self) -> threading.Thread:
        """Start the scheduler background thread.

        Returns:
            The started daemon thread (for lifecycle management by caller).
        """
        self._thread = threading.Thread(
            target=self._run,
            name="daemon-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Task scheduler started — %d task(s), tick=%.0fs",
            len(self._tasks),
            self._tick_interval,
        )
        return self._thread

    def status(self) -> list[dict]:
        """Return serializable status for all registered tasks.

        Returns:
            List of dicts with: name, interval_seconds, last_run (ISO or None),
            last_error, run_count, error_count.
        """
        with self._lock:
            return [
                {
                    "name": t.name,
                    "interval_seconds": t.interval_seconds,
                    "last_run": t.last_run.isoformat() if t.last_run else None,
                    "last_error": t.last_error,
                    "run_count": t.run_count,
                    "error_count": t.error_count,
                }
                for t in self._tasks
            ]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Main scheduler loop — ticks every TICK_INTERVAL seconds."""
        while not self._stop_event.is_set():
            now = datetime.now(timezone.utc)
            with self._lock:
                tasks_snapshot = list(self._tasks)

            for task in tasks_snapshot:
                if task.is_due(now):
                    task.run()

            self._stop_event.wait(timeout=self._tick_interval)


# ---------------------------------------------------------------------------
# Built-in task factories
# ---------------------------------------------------------------------------


def make_memory_promotion_task(home: Path) -> Callable[[], None]:
    """Return a callback that runs an hourly memory promotion sweep.

    Instantiates PromotionEngine lazily (so import errors are deferred until
    first run, matching the graceful-import pattern used elsewhere in the daemon).

    Args:
        home: Agent home directory containing the ``memory/`` subtree.
    """

    def _run() -> None:
        from .memory_promoter import PromotionEngine

        engine = PromotionEngine(home)
        result = engine.sweep()
        if result.promoted:
            logger.info(
                "Memory promotion sweep: %d promoted of %d scanned",
                len(result.promoted),
                result.scanned,
            )
        else:
            logger.debug(
                "Memory promotion sweep: %d scanned, 0 promoted",
                result.scanned,
            )

    return _run


def make_backend_reprobe_task(consciousness_loop: object) -> Callable[[], None]:
    """Return a callback that re-probes LLM backend availability every 5 min.

    Reaches into ``consciousness_loop._bridge._probe_available_backends()``.
    Silently no-ops if any of those attributes are missing, keeping the
    scheduler stable when the consciousness loop is unavailable.

    Args:
        consciousness_loop: ConsciousnessLoop instance (or None).
    """

    def _run() -> None:
        if consciousness_loop is None:
            return
        bridge = getattr(consciousness_loop, "_bridge", None)
        if bridge is None:
            return
        probe_fn = getattr(bridge, "_probe_available_backends", None)
        if callable(probe_fn):
            probe_fn()
            available = getattr(bridge, "_available", {})
            enabled = [k for k, v in available.items() if v]
            logger.debug("Backend re-probe: available=%s", enabled)

    return _run


def make_heartbeat_task(
    beacon: object,
    consciousness_active_fn: Callable[[], bool],
) -> Callable[[], None]:
    """Return a callback that emits a heartbeat pulse every 30 seconds.

    Args:
        beacon: HeartbeatBeacon instance (or None).
        consciousness_active_fn: Zero-arg callable returning bool — whether
            the consciousness loop is currently active.
    """

    def _run() -> None:
        if beacon is None:
            return
        active = consciousness_active_fn()
        beacon.pulse(consciousness_active=active)
        logger.debug("Heartbeat pulse sent (consciousness_active=%s)", active)

    return _run


def make_profile_freshness_task(home: Path, max_age_days: int = 7) -> Callable[[], None]:
    """Return a callback that checks agent profile freshness daily.

    Inspects:
    - ``identity/identity.json``
    - ``data/model_profiles/*.json`` (if present)

    Logs a WARNING for any file older than *max_age_days* days, otherwise
    logs at DEBUG level so the daemon stays quiet on healthy systems.

    Args:
        home: Agent home directory.
        max_age_days: Files older than this trigger a warning (default 7).
    """

    def _run() -> None:
        now = datetime.now(timezone.utc)
        warnings: list[str] = []

        # Identity manifest
        identity_file = home / "identity" / "identity.json"
        if identity_file.exists():
            mtime = datetime.fromtimestamp(identity_file.stat().st_mtime, tz=timezone.utc)
            age_days = (now - mtime).days
            if age_days > max_age_days:
                warnings.append(
                    f"identity.json is {age_days}d old — consider re-running 'skcapstone init'"
                )

        # Model profile files
        profiles_dir = home / "data" / "model_profiles"
        if profiles_dir.exists():
            for profile in sorted(profiles_dir.glob("*.json")):
                mtime = datetime.fromtimestamp(profile.stat().st_mtime, tz=timezone.utc)
                age_days = (now - mtime).days
                if age_days > max_age_days:
                    warnings.append(
                        f"model profile '{profile.stem}' is {age_days}d old"
                    )

        if warnings:
            for msg in warnings:
                logger.warning("Profile freshness: %s", msg)
        else:
            logger.debug("Profile freshness check passed — all profiles current")

    return _run


def make_dreaming_task(
    home: Path, consciousness_loop: object = None
) -> Callable[[], None]:
    """Return a callback that runs the dreaming engine every 15 minutes.

    Instantiates DreamingEngine lazily (so import errors are deferred until
    first run). The engine itself checks idle state and cooldown internally.

    Args:
        home: Agent home directory.
        consciousness_loop: ConsciousnessLoop instance for idle detection.
    """

    def _run() -> None:
        from .consciousness_config import load_dreaming_config
        from .dreaming import DreamingEngine

        config = load_dreaming_config(home)
        if config is None or not config.enabled:
            return
        engine = DreamingEngine(
            home=home, config=config, consciousness_loop=consciousness_loop
        )
        result = engine.dream()
        if result and result.memories_created:
            logger.info(
                "Dreaming: %d memories created from reflection",
                len(result.memories_created),
            )
        elif result and result.skipped_reason:
            logger.debug("Dreaming skipped: %s", result.skipped_reason)

    return _run


# ---------------------------------------------------------------------------
# Convenience builder
# ---------------------------------------------------------------------------


def build_scheduler(
    home: Path,
    stop_event: threading.Event,
    consciousness_loop: object = None,
    beacon: object = None,
    sync_watcher: object = None,
) -> TaskScheduler:
    """Build and register all standard scheduled tasks.

    Tasks registered (in priority order — shortest interval first):

    +--------------------------+------------+
    | Task                     | Interval   |
    +==========================+============+
    | heartbeat_pulse          | 30 s       |
    +--------------------------+------------+
    | sync_inbox_scan          | 30 s       |
    +--------------------------+------------+
    | backend_reprobe          | 5 min      |
    +--------------------------+------------+
    | service_health_check     | 5 min      |
    +--------------------------+------------+
    | memory_promotion_sweep   | 1 hour     |
    +--------------------------+------------+
    | profile_freshness_check  | 24 hours   |
    +--------------------------+------------+
    | dreaming_reflection      | 15 min     |
    +--------------------------+------------+

    Args:
        home: Agent home directory.
        stop_event: Daemon stop event — scheduler thread exits when set.
        consciousness_loop: Optional ConsciousnessLoop for backend re-probe.
        beacon: Optional HeartbeatBeacon for heartbeat pulse.
        sync_watcher: Optional SyncWatcher for inbox polling fallback.

    Returns:
        Configured TaskScheduler (call ``.start()`` to begin).
    """
    scheduler = TaskScheduler(home, stop_event)

    def _consciousness_active() -> bool:
        if consciousness_loop is None:
            return False
        cfg = getattr(consciousness_loop, "_config", None)
        return bool(cfg and getattr(cfg, "enabled", False))

    scheduler.register(
        name="heartbeat_pulse",
        interval_seconds=30,
        callback=make_heartbeat_task(beacon, _consciousness_active),
    )

    # Sync inbox polling (fallback for when inotify misses events)
    try:
        from .sync_watcher import make_sync_inbox_scan_task

        scheduler.register(
            name="sync_inbox_scan",
            interval_seconds=30,
            callback=make_sync_inbox_scan_task(sync_watcher),
        )
    except ImportError:
        logger.debug("sync_watcher not available — sync_inbox_scan task skipped")

    scheduler.register(
        name="backend_reprobe",
        interval_seconds=300,  # 5 minutes
        callback=make_backend_reprobe_task(consciousness_loop),
    )

    scheduler.register(
        name="memory_promotion_sweep",
        interval_seconds=3600,  # 1 hour
        callback=make_memory_promotion_task(home),
    )

    scheduler.register(
        name="profile_freshness_check",
        interval_seconds=86400,  # 24 hours
        callback=make_profile_freshness_task(home),
    )

    # Dreaming — idle-time self-reflection via NVIDIA NIM
    scheduler.register(
        name="dreaming_reflection",
        interval_seconds=900,  # 15 minutes
        callback=make_dreaming_task(home, consciousness_loop),
    )

    # Service health check — pings Qdrant, FalkorDB, Syncthing, daemons
    try:
        from .service_health import make_service_health_task

        scheduler.register(
            name="service_health_check",
            interval_seconds=300,  # 5 minutes
            callback=make_service_health_task(),
        )
    except ImportError:
        logger.debug("service_health not available — service_health_check task skipped")

    return scheduler
