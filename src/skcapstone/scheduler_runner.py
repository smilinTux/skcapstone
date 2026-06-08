"""Executes JobSpecs by type (python | shell | agent) with overlap locking.

This module is the execution layer for the unified fleet job scheduler.  It
is intentionally free of scheduling logic — callers decide *when* to run a
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
import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

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

    @contextlib.contextmanager
    def lock(self, job: JobSpec) -> Generator[bool, None, None]:
        """Acquire an exclusive per-job overlap lock.

        Uses an ``O_CREAT | O_EXCL`` open on a ``<job.name>.lock`` file as
        an atomic test-and-set.  The lock is always released when the
        context exits, even if the body raises.

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
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            yield False
            return
        try:
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            yield True
        finally:
            # NOTE: if the process is SIGKILL'd or the host crashes, this unlink
            # never runs and the lockfile blocks the job until removed. The PID
            # written above is the hook for a future staleness check (compare to
            # /proc/<pid> and unlink if the process is gone); v1 relies on
            # operators clearing stale locks on restart.
            with contextlib.suppress(OSError):
                lock_path.unlink()

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def run(self, job: JobSpec) -> JobResult:
        """Execute a job and return a :class:`JobResult`.

        Dispatches to the appropriate backend based on ``job.type``:

        - ``"python"`` — imports ``module`` and calls ``fn()`` from
          ``job.callback`` (format: ``"module.path:function_name"``).
        - ``"shell"`` — runs ``job.command`` via :mod:`subprocess` after
          splitting with :func:`shlex.split`.
        - ``"agent"`` — runs ``claude -p "<prompt>"`` optionally with
          ``--agent <name>``.

        Jobs *never* raise — all failures are returned as a
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
                    error=f"invalid callback {job.callback!r} — expected 'module:fn'",
                )
            module = importlib.import_module(mod_name)
            fn = getattr(module, fn_name)
            fn()
            return JobResult(ok=True)
        except Exception as exc:  # noqa: BLE001 — jobs must never crash the scheduler loop
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
