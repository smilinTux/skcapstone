"""Executes JobSpecs by type (python | shell | agent) with overlap locking.

This module is the execution layer for the unified fleet job scheduler.  It
is intentionally free of scheduling logic - callers decide *when* to run a
job; this module handles the *how*.

Typical usage::

    from pathlib import Path
    from skcapstone.scheduler_jobs import JobSpec
    from skcapstone.scheduler_runner import JobRunner

    runner = JobRunner(log_dir=Path("~/.skcapstone/logs").expanduser())
    with runner.lock(job) as acquired:
        if acquired:
            result = runner.run(job)
"""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
import os
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional

from .scheduler_jobs import JobSpec

logger = logging.getLogger("skcapstone.scheduler_runner")


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------


@dataclass
class JobResult:
    """Captures the outcome of a single job execution.

    Attributes:
        ok: ``True`` when the job completed successfully (exit code 0 for
            subprocesses, no exception for python callbacks).
        exit_code: Process exit code for subprocess-based jobs.  ``0`` for
            successful python jobs; ``-1`` for timeouts or OS errors.
        output: Combined stdout + stderr captured from subprocess jobs.
            Empty for python-callback jobs.
        error: Human-readable error message on failure.  Empty string on
            success.
    """

    ok: bool
    exit_code: int = 0
    output: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class JobRunner:
    """Executes :class:`~skcapstone.scheduler_jobs.JobSpec` instances.

    Each runner owns a ``log_dir`` directory where per-run log files and
    per-job lock files are written.

    Args:
        log_dir: Directory for run logs and overlap-lock files.  Created
            automatically if it does not exist.
    """

    def __init__(self, log_dir: Path) -> None:
        """Initialise the runner with a log directory.

        Args:
            log_dir: Writable directory for logs and lock files.  Will be
                created (with parents) on first use.
        """
        self.log_dir = Path(log_dir)

    # ------------------------------------------------------------------
    # Overlap lock
    # ------------------------------------------------------------------

    # Bounded stale recovery: an unrecognised lock (unreadable, pre-v1 format,
    # or written by a process we cannot verify) is never treated as a live
    # holder - it is yielded to a new owner once it has outlived this window.
    # A recognised lock whose owner is provably gone is recovered
    # immediately. This bounds the worst-case block after a crash to
    # STALE_LOCK_MAX_AGE_SECONDS instead of "forever" (audit a8200004).
    STALE_LOCK_MAX_AGE_SECONDS = float(os.environ.get("SKCAPSTONE_LOCK_STALE_SECONDS", "600"))

    @contextlib.contextmanager
    def lock(self, job: JobSpec) -> Generator[bool, None, None]:
        """Acquire an exclusive per-job overlap lock.

        Uses an ``O_CREAT | O_EXCL`` open on a ``<job.name>.lock`` file as
        an atomic test-and-set.  The lock is always released when the
        context exits, even if the body raises.

        Ownership is established by writing a JSON record
        (``{"v": 1, "pid": <int>, "btime": <int>, "token": <uuid>}``),
        not a bare PID.  A held lock is only treated as stale when its
        owner is provably gone: no process with that PID, or a process
        with that PID whose boot time (``/proc/<pid>/stat`` field 22) does
        not match the recorded one.  A live process that merely reused the
        PID therefore never blocks the job, and a genuinely live owner
        is never stolen (overlap safety is preserved - the atomic
        ``O_EXCL`` create is the only path to ownership).  Locks we
        cannot validate are recovered once they outlive
        ``STALE_LOCK_MAX_AGE_SECONDS`` (bounded stale recovery).

        Args:
            job: The job whose lock should be acquired.

        Yields:
            ``True`` if the lock was acquired; ``False`` if another instance
            already holds it (the caller should skip this run).

        Example::

            with runner.lock(job) as acquired:
                if acquired:
                    result = runner.run(job)
                else:
                    logger.info("job %s already running, skipping", job.name)
        """
        self.log_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.log_dir / f"{job.name}.lock"
        acquired = False
        record_bytes = b""
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            verdict = self._classify_lock(lock_path)
            if verdict != self._STALE:
                # A verified live owner, or an unverified owner still within
                # the bounded recovery window, must remain untouched.  In
                # particular, never unlink a live lock: that would defeat
                # overlap exclusion.
                yield False
                return
            logger.warning(
                "Clearing stale lock for job %s (%s): %s",
                job.name,
                lock_path,
                "owning process is gone or stale window elapsed",
            )
            with contextlib.suppress(OSError):
                lock_path.unlink()
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                # Lost the re-acquire race to a concurrent contender.  That
                # contender holds the lock, so skip this run.
                yield False
                return
        try:
            record_bytes = json.dumps(
                {
                    "v": 1,
                    "pid": os.getpid(),
                    "btime": self._own_boot_time(),
                    "token": str(uuid.uuid4()),
                }
            ).encode("utf-8")
            os.write(fd, record_bytes)
            os.close(fd)
            acquired = True
            yield True
        finally:
            if acquired:
                # Release by identity: unlink only if the lock still holds the
                # record WE wrote. If a bounded-recovery path already replaced
                # our record, our unlink must not delete the new owner's lock.
                # (If we were SIGKILL'd, this unlink never runs - the recorded
                # PID + boot time are exactly what the next acquirer checks.)
                with contextlib.suppress(OSError):
                    if lock_path.read_bytes() == record_bytes:
                        lock_path.unlink()

    # Lock verdicts.
    _STALE = "stale"        # provably dead owner, or bounded window elapsed
    _LIVE = "live"          # provably live owner - never touch the lock
    _UNKNOWN = "unknown"    # could not validate - bounded window applies

    @classmethod
    def _classify_lock(cls, lock_path: Path) -> str:
        """Classify an existing lock file as stale, live, or unknown.

        Args:
            lock_path: Path to an existing ``<job.name>.lock`` file.

        Returns:
            ``_STALE`` when the recorded owner is provably gone (no such
            PID, or the PID exists with a different boot time - i.e. the
            PID was reused by another process) or the record has outlived
            ``STALE_LOCK_MAX_AGE_SECONDS``.  ``_LIVE`` when the recorded
            owner is provably alive (same PID AND same boot time).  ``_UNKNOWN``
            when the record cannot be read or parsed, or the owner process
            cannot be inspected (non-Linux, permission denied, vanished
            between checks).
        """
        try:
            raw = lock_path.read_bytes()
        except OSError:
            raw = b""
        record = None
        try:
            record = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            record = None

        if isinstance(record, dict) and record.get("v") == 1:
            pid = record.get("pid")
            btime = record.get("btime")
            token = record.get("token")
            try:
                uuid.UUID(token)
            except (AttributeError, ValueError, TypeError):
                token = None
            if token is not None and isinstance(pid, int) and isinstance(btime, int) and pid > 0:
                state = cls._owner_state(pid, btime)
                if state == "gone":
                    return cls._STALE
                if state == "alive":
                    return cls._LIVE
            # Record present but incomplete or uncheckable -> bounded window.
            if cls._age_seconds(lock_path) > cls.STALE_LOCK_MAX_AGE_SECONDS:
                return cls._STALE
            return cls._UNKNOWN

        # No recognisable record (pre-v1 PID-only format or garbage):
        # validate the bare PID if we can, otherwise bounded window.
        try:
            legacy_pid = int(raw.decode("utf-8", errors="strict").strip())
        except (ValueError, UnicodeDecodeError):
            legacy_pid = 0
        if legacy_pid > 0:
            if cls._owner_state(legacy_pid, None) == "gone":
                return cls._STALE
        if cls._age_seconds(lock_path) > cls.STALE_LOCK_MAX_AGE_SECONDS:
            return cls._STALE
        return cls._UNKNOWN

    @staticmethod
    def _owner_state(pid: int, btime: Optional[int]) -> str:
        """Check whether the recorded owner is still the same process.

        Args:
            pid: PID recorded in the lock.
            btime: Boot-time field (``/proc/<pid>/stat`` field 22) recorded
                at acquire time, or ``None`` for legacy PID-only records
                where only liveness can be checked.

        Returns:
            ``"gone"`` when no process with that PID exists.  ``"alive"``
            when a process with that PID exists and, when ``btime`` is
            given, its boot time matches.  ``"unknown"`` on any other
            ambiguity (permission denied, unreadable stat, process that
            vanished mid-check).
        """
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "gone"
        except OSError:
            return "unknown"
        if btime is None:
            return "unknown"  # legacy record: liveness confirmed, identity not
        proc_stat = Path("/proc") / str(pid) / "stat"
        try:
            fields = proc_stat.read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()
            # /proc/<pid>/stat field 22 is the process start time; after
            # dropping "pid (comm)" it lands at index 22 - 3 = 19.
            owner_btime = int(fields[19])
        except (OSError, ValueError, IndexError):
            return "unknown"
        return "alive" if owner_btime == btime else "gone"

    @staticmethod
    def _age_seconds(lock_path: Path) -> float:
        """Return the lock file's age in seconds (0.0 if unreadable)."""
        try:
            return max(0.0, time.time() - lock_path.stat().st_mtime)
        except OSError:
            return 0.0

    @staticmethod
    def _own_boot_time() -> int:
        """Return this process's boot-time field from ``/proc/self/stat``.

        Returns:
            The start-time field (index 22) as an int. Falls back to ``0``
            on non-Linux hosts or read failures; a ``0`` never matches a
            real recorded boot time, which degrades the lock to "unknown"
            (bounded recovery) rather than stealing a live owner.
        """
        try:
            fields = Path("/proc/self/stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()
            return int(fields[19])
        except (OSError, ValueError, IndexError):
            return 0

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def run(self, job: JobSpec) -> JobResult:
        """Execute a job and return a :class:`JobResult`.

        Dispatches to the appropriate backend based on ``job.type``:

        - ``"python"`` - imports ``module`` and calls ``fn()`` from
          ``job.callback`` (format: ``"module.path:function_name"``).
        - ``"shell"`` - runs ``job.command`` via :mod:`subprocess` after
          splitting with :func:`shlex.split`.
        - ``"agent"`` - runs ``claude -p "<prompt>"`` optionally with
          ``--agent <name>``.

        Jobs *never* raise - all failures are returned as a
        :class:`JobResult` with ``ok=False``.

        Args:
            job: The job specification to execute.

        Returns:
            A :class:`JobResult` describing the outcome.
        """
        if job.type == "python":
            return self._run_python(job)
        if job.type == "shell":
            return self._run_subprocess(job, shlex.split(job.command or ""))
        if job.type == "agent":
            cmd = ["claude", "-p", job.prompt or ""]
            if job.agent:
                cmd += ["--agent", job.agent]
            return self._run_subprocess(job, cmd)
        return JobResult(ok=False, error=f"unknown job type: {job.type!r}")

    # ------------------------------------------------------------------
    # Private backends
    # ------------------------------------------------------------------

    def _run_python(self, job: JobSpec) -> JobResult:
        """Import and call a ``module:function`` callback.

        Args:
            job: A python-type :class:`~skcapstone.scheduler_jobs.JobSpec`
                whose ``callback`` field is ``"module.path:fn_name"``.

        Returns:
            :class:`JobResult` with ``ok=True`` on success, or ``ok=False``
            with ``error`` set to the exception message on any failure.
        """
        try:
            mod_name, _, fn_name = (job.callback or "").partition(":")
            if not mod_name or not fn_name:
                return JobResult(
                    ok=False,
                    error=f"invalid callback {job.callback!r} - expected 'module:fn'",
                )
            module = importlib.import_module(mod_name)
            fn = getattr(module, fn_name)
            fn()
            return JobResult(ok=True)
        except Exception as exc:  # noqa: BLE001 - jobs must never crash the scheduler loop
            logger.error("python job %r failed: %s", job.name, exc, exc_info=True)
            return JobResult(ok=False, error=str(exc))

    def _run_subprocess(self, job: JobSpec, cmd: list[str]) -> JobResult:
        """Run *cmd* as a subprocess, capturing output to a timestamped log.

        Args:
            job: The originating :class:`~skcapstone.scheduler_jobs.JobSpec`
                (used for log file naming and timeout).
            cmd: Argument list passed directly to :class:`subprocess.run`.

        Returns:
            :class:`JobResult` with:

            - ``ok=True`` and ``exit_code=0`` on success.
            - ``ok=False`` and ``exit_code=<n>`` on nonzero exit.
            - ``ok=False`` and ``exit_code=-1`` on timeout or OS error.
        """
        self.log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = self.log_dir / f"{job.name}-{ts}.log"
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=job.timeout,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            log_path.write_text(out, encoding="utf-8")
            ok = proc.returncode == 0
            return JobResult(
                ok=ok,
                exit_code=proc.returncode,
                output=out,
                error="" if ok else out[-500:],
            )
        except subprocess.TimeoutExpired:
            logger.error("job %r timed out after %ss", job.name, job.timeout)
            return JobResult(ok=False, exit_code=-1, error=f"timeout after {job.timeout}s")
        except (OSError, ValueError) as exc:
            logger.error("job %r subprocess error: %s", job.name, exc)
            return JobResult(ok=False, exit_code=-1, error=str(exc))
