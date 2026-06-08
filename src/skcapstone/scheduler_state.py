"""Node-local (never-synced) state for the skscheduler.

This module provides per-host, per-job run state that is intentionally kept
node-local so it never becomes a Syncthing conflict source.  State is stored
at ``<root>/scheduler/<hostname>/state.json`` and is never replicated.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.scheduler_state")


class SchedulerState:
    """Per-host job state persisted at ``<root>/scheduler/<hostname>/state.json``.

    State is deliberately node-local: the file lives outside any Syncthing-
    watched subtree so the scheduler never races with sync.  Each instance
    reads from disk on construction and writes through on every
    :meth:`record_run` call — there is no in-process cache staleness issue
    because schedulers are single-process per host.

    Attributes:
        state_file: Absolute path to the JSON state file for this host.
    """

    def __init__(self, root: Path, hostname: str) -> None:
        """Initialise state for ``hostname`` rooted at ``root``.

        Reads any existing state from disk.  If the file is absent or
        unreadable the state is treated as empty rather than raising.

        Args:
            root: Repository (or data) root directory.  The state file will
                be created at ``root/scheduler/<hostname>/state.json``.
            hostname: Identifier for this node (typically
                ``socket.gethostname()``).  Used as the directory name so
                multiple hosts can share the same ``root`` without collision.
        """
        self.state_file: Path = Path(root) / "scheduler" / hostname / "state.json"
        self._data: dict[str, dict] = {}
        self._write_lock = threading.Lock()
        if self.state_file.exists():
            try:
                self._data = json.loads(
                    self.state_file.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "Could not read scheduler state from %s: %s", self.state_file, exc
                )
                self._data = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, job: str) -> dict:
        """Return the state record for *job*, or a zeroed default.

        The returned dict always contains at least the keys
        ``run_count``, ``error_count``, and ``last_run``.  Additional keys
        (``last_status``, ``last_error``) are present once the job has been
        recorded at least once.

        Args:
            job: Unique job identifier string.

        Returns:
            A copy-on-read dict with the job's state.  Mutating the returned
            dict does **not** persist anything; call :meth:`record_run` to
            persist changes.
        """
        return self._data.get(
            job, {"run_count": 0, "error_count": 0, "last_run": None}
        )

    def last_run(self, job: str) -> Optional[datetime]:
        """Return the timestamp of the most recent run of *job*, or ``None``.

        The returned :class:`~datetime.datetime` is always timezone-aware
        (UTC) because :meth:`record_run` stores ISO-8601 strings with a
        ``+00:00`` offset.

        Args:
            job: Unique job identifier string.

        Returns:
            A timezone-aware :class:`~datetime.datetime` if the job has run
            at least once, otherwise ``None``.
        """
        raw: Optional[str] = self.get(job).get("last_run")
        return datetime.fromisoformat(raw) if raw else None

    def record_run(
        self,
        job: str,
        now: Optional[datetime] = None,
        ok: bool = True,
        error: str = "",
    ) -> None:
        """Record the result of a job execution and persist to disk.

        Increments either ``run_count`` (on success) or ``error_count`` (on
        failure) and writes the updated state file atomically via
        :meth:`_flush`.

        Args:
            job: Unique job identifier string.
            now: Timestamp for the run.  Defaults to
                ``datetime.now(timezone.utc)`` when not provided.
            ok: ``True`` if the job completed successfully, ``False`` on
                error.
            error: Human-readable error message.  Ignored when *ok* is
                ``True``; stored as ``last_error`` otherwise.
        """
        ts: datetime = now or datetime.now(timezone.utc)
        with self._write_lock:
            rec: dict = self.get(job)
            rec["last_run"] = ts.isoformat()
            rec["last_status"] = "ok" if ok else "error"
            rec["last_error"] = "" if ok else error
            rec["run_count"] = rec.get("run_count", 0) + (1 if ok else 0)
            rec["error_count"] = rec.get("error_count", 0) + (0 if ok else 1)
            self._data[job] = rec
            self._flush()

    def all(self) -> dict[str, dict]:
        """Return a shallow copy of all job state records.

        Useful for introspection and dashboards.  Mutations to the returned
        dict or its values do not affect persisted state.

        Returns:
            A dict mapping job identifier → state record for every job that
            has been recorded at least once.
        """
        return dict(self._data)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _flush(self) -> None:
        """Write the in-memory state to :attr:`state_file`.

        Creates parent directories if they do not exist.  Writes the full
        state dict as indented JSON followed by a trailing newline so the
        file is human-readable and POSIX-compliant.

        Raises:
            OSError: If the file cannot be written (e.g. permission denied).
        """
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(self._data, indent=2) + "\n", encoding="utf-8"
        )
