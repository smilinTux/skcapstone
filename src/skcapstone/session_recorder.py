"""
SKCapstone Session Recorder — capture MCP tool calls + responses as JSONL.

Each MCP session is auto-saved to ~/.skcapstone/sessions/ and rotated to
keep the last 5.  An explicit output path can be set via SKCAPSTONE_RECORD_FILE
or the ``--output`` flag on ``skcapstone record``.

JSONL line schema::

    {
      "ts":          "2026-03-02T10:00:00.123456+00:00",   # ISO-8601 UTC
      "tool":        "memory_store",
      "arguments":   {"content": "...", "tags": [...]},
      "result":      [{"type": "text", "text": "..."}],
      "duration_ms": 45
    }
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("skcapstone.session_recorder")

_SESSIONS_KEEP = 5
_SESSIONS_SUBDIR = "sessions"


def _sessions_dir(home: Path) -> Path:
    """Return the sessions directory, creating it if absent."""
    d = home / _SESSIONS_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _auto_rotate(sessions_dir: Path, keep: int = _SESSIONS_KEEP) -> None:
    """Delete old auto-session files to keep only the most recent *keep*."""
    auto_files = sorted(
        sessions_dir.glob("session-*.jsonl"),
        key=lambda p: p.stat().st_mtime,
    )
    for old in auto_files[: max(0, len(auto_files) - keep)]:
        try:
            old.unlink()
            logger.debug("Rotated old session: %s", old)
        except OSError:
            pass


class SessionRecorder:
    """Records MCP tool calls and responses to one or two JSONL sinks.

    Args:
        home:        Agent home directory (``~/.skcapstone`` or agent-specific).
        output_path: Optional explicit output file.  If *None* only the
                     auto-session file is written.
    """

    def __init__(self, home: Path, output_path: Optional[Path] = None) -> None:
        self._home = home
        self._output_path = output_path
        self._auto_path: Optional[Path] = None
        self._auto_fh = None
        self._output_fh = None
        self._count = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @classmethod
    def start_session(
        cls,
        home: Path,
        output_path: Optional[Path] = None,
    ) -> "SessionRecorder":
        """Factory: open files and return a ready recorder.

        Checks ``SKCAPSTONE_RECORD_FILE`` env var when *output_path* is None.
        """
        env_path = os.environ.get("SKCAPSTONE_RECORD_FILE")
        if output_path is None and env_path:
            output_path = Path(env_path).expanduser()

        rec = cls(home, output_path)
        rec._open()
        return rec

    def _open(self) -> None:
        sessions_dir = _sessions_dir(self._home)
        now = datetime.now(timezone.utc)
        # Include microseconds + PID so rapid test runs produce distinct filenames.
        ts = now.strftime("%Y%m%dT%H%M%S") + f"-{now.microsecond:06d}-{os.getpid()}"
        self._auto_path = sessions_dir / f"session-{ts}.jsonl"
        self._auto_fh = open(self._auto_path, "w", encoding="utf-8")  # noqa: WPS515
        logger.debug("Session recorder: auto-save → %s", self._auto_path)

        if self._output_path:
            self._output_path.parent.mkdir(parents=True, exist_ok=True)
            self._output_fh = open(self._output_path, "w", encoding="utf-8")  # noqa: WPS515
            logger.debug("Session recorder: output  → %s", self._output_path)

    def close(self) -> None:
        """Flush, close, and rotate old session files."""
        for fh in (self._auto_fh, self._output_fh):
            if fh:
                try:
                    fh.flush()
                    fh.close()
                except OSError:
                    pass
        self._auto_fh = None
        self._output_fh = None

        if self._auto_path:
            _auto_rotate(_sessions_dir(self._home), keep=_SESSIONS_KEEP)
        logger.info(
            "Session recorder closed: %d tool call(s) recorded", self._count
        )

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(
        self,
        tool: str,
        arguments: dict[str, Any],
        result: list[Any],
        duration_ms: int,
    ) -> None:
        """Append one JSONL line to all open sinks."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "arguments": arguments,
            "result": _serialise_result(result),
            "duration_ms": duration_ms,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        for fh in (self._auto_fh, self._output_fh):
            if fh:
                fh.write(line)
                fh.flush()
        self._count += 1

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @property
    def auto_path(self) -> Optional[Path]:
        """Path to the auto-session file (set after _open())."""
        return self._auto_path

    @property
    def count(self) -> int:
        """Number of tool calls recorded so far."""
        return self._count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialise_result(result: Any) -> Any:
    """Convert MCP TextContent objects to plain dicts for JSON serialisation."""
    if isinstance(result, list):
        return [_serialise_result(r) for r in result]
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if hasattr(result, "__dict__"):
        return vars(result)
    return result


# ---------------------------------------------------------------------------
# Listing helpers (used by CLI)
# ---------------------------------------------------------------------------


def list_sessions(home: Path) -> list[Path]:
    """Return session files newest-first."""
    sessions_dir = _sessions_dir(home)
    return sorted(
        sessions_dir.glob("session-*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def load_session(path: Path) -> list[dict[str, Any]]:
    """Parse a JSONL session file into a list of entries."""
    entries: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    logger.warning("Skipping malformed JSONL line: %s", exc)
    return entries
