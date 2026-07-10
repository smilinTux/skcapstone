"""
Cron-like scheduler for recurring agent background tasks.

Runs a single daemon thread that wakes up every TICK_INTERVAL seconds,
checks which tasks are due, and fires their callbacks.

Built-in recurring tasks:
    - heartbeat_pulse        — every 30 seconds
    - backend_reprobe        — every 5 minutes
    - memory_promotion_sweep — every hour
    - profile_freshness_check — every 24 hours

Dreaming moved to a jobs.yaml config job (dreaming-reflection) on 2026-07-09 —
see docs/superpowers/plans/2026-07-09-dreaming-skscheduler-migration.md.

Usage:
    scheduler = build_scheduler(home, stop_event, consciousness_loop, beacon)
    thread = scheduler.start()          # returns the daemon thread
    print(scheduler.status())           # list[dict] of task state
"""

from __future__ import annotations

import logging
import os
import random
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .scheduler_jobs import JobSpec, is_due, job_runs_here
from .scheduler_runner import JobRunner
from .scheduler_state import SchedulerState

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
    delay_first_run: float = 0.0

    def is_due(self, now: Optional[datetime] = None) -> bool:
        """Return True if the task interval has elapsed since last_run.

        A task with no prior run is always considered due, unless
        ``delay_first_run`` is set — in that case the first run is
        deferred by that many seconds from process start.

        Args:
            now: Reference time for the check (defaults to UTC now).
        """
        if self.last_run is None:
            if self.delay_first_run > 0:
                if not hasattr(self, "_created_at"):
                    object.__setattr__(self, "_created_at", datetime.now(timezone.utc))
                reference = now or datetime.now(timezone.utc)
                elapsed = (reference - self._created_at).total_seconds()
                return elapsed >= self.delay_first_run
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
        self._config_jobs: list[JobSpec] = []
        self._host_aliases: set[str] = set()
        self._state: Optional[SchedulerState] = None
        self._job_runner: Optional[JobRunner] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        interval_seconds: float,
        callback: Callable[[], None],
        delay_first_run: float = 0.0,
    ) -> ScheduledTask:
        """Register a recurring task.

        Args:
            name: Unique task name (used in logs and status output).
            interval_seconds: Minimum seconds between executions.
            callback: Zero-argument callable to invoke.
            delay_first_run: Seconds to wait before first execution (default 0 = immediate).

        Returns:
            The created ScheduledTask (caller may inspect it at runtime).
        """
        task = ScheduledTask(name=name, interval_seconds=interval_seconds, callback=callback, delay_first_run=delay_first_run)
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

    def load_config_jobs(
        self,
        jobs: list[JobSpec],
        hostname: str,
        host_aliases: set[str],
        state_root: Path,
    ) -> None:
        """Load config-driven jobs and initialise per-host execution state.

        Filters *jobs* to only those that are enabled and whose node affinity
        matches *host_aliases*.  Initialises a :class:`SchedulerState` for
        tracking run history and a :class:`JobRunner` for dispatching jobs.

        **Call before** :meth:`start`.  The attributes ``_config_jobs``,
        ``_state``, and ``_job_runner`` are not lock-protected against
        concurrent mutation while the scheduler thread is running.
        ``build_scheduler`` already calls this before ``start()``, so
        documenting this constraint is sufficient for v1.

        Args:
            jobs: Full list of :class:`~skcapstone.scheduler_jobs.JobSpec`
                instances as returned by
                :func:`~skcapstone.scheduler_jobs.load_jobs`.
            hostname: The current host's primary identifier (typically
                ``socket.gethostname()``), used as the state sub-directory.
            host_aliases: Full set of aliases for the current host, used for
                node-affinity matching via
                :func:`~skcapstone.scheduler_jobs.job_runs_here`.
            state_root: Root directory under which per-host scheduler state
                (``scheduler/<hostname>/state.json``) and log files are stored.
        """
        self._host_aliases = host_aliases
        self._state = SchedulerState(root=state_root, hostname=hostname)
        self._job_runner = JobRunner(
            log_dir=state_root / "scheduler" / hostname / "logs"
        )
        self._config_jobs = [
            j for j in jobs if j.enabled and job_runs_here(j, host_aliases)
        ]
        logger.info(
            "Loaded %d config job(s) for host %s",
            len(self._config_jobs),
            hostname,
        )

    def tick_config_jobs(self, now: Optional[datetime] = None) -> None:
        """Fire any config-driven jobs that are due at *now*.

        Skips silently when no config jobs are loaded or state/runner are not
        initialised (i.e. :meth:`load_config_jobs` has not been called).

        Each due job is dispatched to its own short-lived daemon thread so
        the tick returns immediately.  Long-running jobs (e.g. ``agent``
        type, timeout up to 900 s) therefore never block the scheduler daemon
        thread — which also drives heartbeats and all built-in tasks.

        The due-check is intentionally kept in the tick thread (it is cheap).
        The overlap lock is acquired *inside* the worker thread so it spans
        the actual run; :meth:`_run_config_job` handles lock + run + state.

        Note: because ``record_run`` is called asynchronously inside the
        worker, the next tick may evaluate the same job as "due" before
        ``record_run`` completes.  The per-job overlap lock prevents a second
        concurrent execution in that window — the second worker acquires
        ``got=False`` and returns immediately.  :class:`SchedulerState` uses
        a ``threading.Lock`` so concurrent ``record_run`` calls are safe.

        Args:
            now: Reference UTC timestamp for due-checks.  Defaults to
                ``datetime.now(timezone.utc)`` when not provided.
        """
        if not self._config_jobs or self._state is None or self._job_runner is None:
            return
        now = now or datetime.now(timezone.utc)
        for job in self._config_jobs:
            if not is_due(job, self._state.last_run(job.name), now):
                continue
            threading.Thread(
                target=self._run_config_job,
                args=(job, now),
                name=f"skjob-{job.name}",
                daemon=True,
            ).start()

    def _run_config_job(self, job: JobSpec, fire_time: datetime) -> None:
        """Run a single config job in its own thread: lock, execute, record.

        This method is the body of the per-job daemon thread spawned by
        :meth:`tick_config_jobs`.  It acquires the per-job overlap lock,
        runs the job via the configured :class:`~skcapstone.scheduler_runner.JobRunner`,
        then records the result via
        :class:`~skcapstone.scheduler_state.SchedulerState`.

        If the lock cannot be obtained the method returns immediately without
        running or recording — this is the safe path when the previous run is
        still in progress (which can happen if a job's execution time exceeds
        one tick interval).

        Args:
            job: The :class:`~skcapstone.scheduler_jobs.JobSpec` to execute.
            fire_time: The UTC timestamp at which this job was determined to be
                due (propagated to :meth:`~skcapstone.scheduler_state.SchedulerState.record_run`
                so state timestamps reflect the scheduled fire time rather than
                the wall-clock time of completion).
        """
        with self._job_runner.lock(job) as got:
            if not got:
                logger.debug("job '%s' still running — skip", job.name)
                return
            # Jitter: random splay before dispatch so fleet nodes sharing a cron
            # slot don't stampede a shared resource (LLM endpoint, registry, etc).
            if getattr(job, "jitter", 0.0) > 0:
                time.sleep(random.uniform(0.0, float(job.jitter)))
            # Run with retries + linear backoff for transient infra failures.
            attempts = max(1, int(getattr(job, "retries", 0)) + 1)
            result = None
            for i in range(attempts):
                result = self._job_runner.run(job)
                if result.ok:
                    break
                if i < attempts - 1:
                    logger.warning(
                        "job '%s' attempt %d/%d failed: %s — retrying",
                        job.name, i + 1, attempts, result.error,
                    )
                    backoff = float(getattr(job, "retry_backoff", 0.0))
                    if backoff > 0:
                        time.sleep(backoff)
            self._state.record_run(
                job.name, now=fire_time, ok=result.ok, error=result.error
            )
            if not result.ok:
                logger.warning(
                    "job '%s' failed after %d attempt(s): %s",
                    job.name, attempts, result.error,
                )
            self._maybe_notify(job, result, attempts)

    @staticmethod
    def _maybe_notify(job: JobSpec, result, attempts: int) -> None:
        """Fire an sk-alert per the job's ``notify`` policy.

        Policy values: ``off`` (default), ``on_failure``, ``on_success``,
        ``always``.  Sends the job name, status, attempt count, and a tail of
        the captured output to Chef's Telegram via the ``sk-alert`` primitive.
        Never raises — notification failure must not break the scheduler.

        Args:
            job: The job that ran.
            result: The :class:`~skcapstone.scheduler_runner.JobResult`.
            attempts: Number of attempts made (for the message).
        """
        mode = getattr(job, "notify", "off")
        if mode == "off":
            return
        want = (
            mode == "always"
            or (mode == "on_failure" and not result.ok)
            or (mode == "on_success" and result.ok)
        )
        if not want:
            return
        status = "✅ ok" if result.ok else "❌ FAILED"
        suffix = f" (after {attempts} attempts)" if attempts > 1 else ""
        tail = "\n".join((result.output or result.error or "").strip().splitlines()[-12:])
        msg = f"🗓️ skscheduler · {job.name} · {status}{suffix}"
        if tail:
            msg += "\n" + tail
        level = "info" if result.ok else getattr(job, "notify_level", "warn")
        alert = shutil.which("sk-alert") or os.path.expanduser("~/.skenv/bin/sk-alert")
        try:
            subprocess.run([alert, "-l", level, msg], timeout=30, check=False)
        except Exception as exc:  # noqa: BLE001 — notify must never break the loop
            logger.warning("notify failed for job '%s': %s", job.name, exc)

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

            self.tick_config_jobs(now)

            self._stop_event.wait(timeout=self._tick_interval)


# ---------------------------------------------------------------------------
# Built-in task factories
# ---------------------------------------------------------------------------


def make_memory_promotion_task(home: Path) -> Callable[[], None]:
    """Return a callback that runs an hourly memory promotion sweep.

    The sweep runs in a dedicated background thread so it never blocks the
    scheduler (and therefore never blocks watchdog pings or other scheduled
    tasks).  A ``threading.Event`` gate prevents overlapping sweeps.

    The sweep is rate-limited to 50 promotions per run to bound I/O time.

    Args:
        home: Agent home directory containing the ``memory/`` subtree.
    """
    _running = threading.Event()

    def _sweep() -> None:
        try:
            from .memory_promoter import PromotionEngine

            engine = PromotionEngine(home)
            result = engine.sweep(limit=50)
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
        except Exception as exc:
            logger.error("Memory promotion sweep error: %s", exc)
        finally:
            _running.clear()

    def _run() -> None:
        if _running.is_set():
            logger.debug("Memory promotion sweep already running — skipping")
            return
        _running.set()
        t = threading.Thread(
            target=_sweep,
            name="memory-promotion-sweep",
            daemon=True,
        )
        t.start()

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


def make_itil_auto_close_task(home: Path) -> Callable[[], None]:
    """Return a callback that auto-closes resolved incidents after 24h stable.

    Args:
        home: Shared root directory.
    """

    def _run() -> None:
        from .itil import ITILManager

        mgr = ITILManager(home)
        closed = mgr.auto_close_resolved(stable_hours=24)
        if closed:
            logger.info("ITIL auto-close: %d incident(s) closed: %s", len(closed), closed)
        else:
            logger.debug("ITIL auto-close: no incidents to close")

    return _run


def make_itil_escalation_task(home: Path) -> Callable[[], None]:
    """Return a callback that checks SLA breaches on open incidents.

    Args:
        home: Shared root directory.
    """

    def _run() -> None:
        from .itil import ITILManager

        mgr = ITILManager(home)
        breaches = mgr.check_sla_breaches()
        if breaches:
            for b in breaches:
                logger.warning(
                    "ITIL SLA breach: %s (%s) unacknowledged for %d min (limit: %d min)",
                    b["id"], b["severity"], b["elapsed_minutes"], b["sla_minutes"],
                )
        else:
            logger.debug("ITIL escalation check: no SLA breaches")

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
        delay_first_run=120,  # let daemon stabilize before first sweep
    )

    scheduler.register(
        name="profile_freshness_check",
        interval_seconds=86400,  # 24 hours
        callback=make_profile_freshness_task(home),
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

    # ITIL escalation check — SLA breach detection every 5 minutes
    try:
        from . import SHARED_ROOT

        shared = Path(SHARED_ROOT).expanduser()
        scheduler.register(
            name="itil_escalation_check",
            interval_seconds=300,  # 5 minutes
            callback=make_itil_escalation_task(shared),
        )
        scheduler.register(
            name="itil_auto_close",
            interval_seconds=1800,  # 30 minutes
            callback=make_itil_auto_close_task(shared),
        )
    except Exception:
        logger.debug("ITIL scheduled tasks not available — skipped")

    import socket

    from .scheduler_jobs import current_host_aliases, load_jobs_with_dropins
    jobs_path = Path(home) / "config" / "jobs.yaml"
    jobs = load_jobs_with_dropins(jobs_path)
    if jobs:
        scheduler.load_config_jobs(
            jobs=jobs,
            hostname=socket.gethostname(),
            host_aliases=current_host_aliases(),
            state_root=Path(home),
        )

    return scheduler
