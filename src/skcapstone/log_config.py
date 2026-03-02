"""Structured JSON logging with rotation for the skcapstone daemon."""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path

# Attributes present on every LogRecord — excluded from JSON extra-field pass-through.
_LOG_RECORD_BUILTIN_ATTRS: frozenset[str] = frozenset(
    {
        "args",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)

# Module-level guard so configure_logging() is idempotent.
_CONFIGURED: bool = False


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects.

    Mandatory fields in every record:

    - ``ts``     — ISO-8601 UTC timestamp
    - ``level``  — log level name (e.g. ``"INFO"``)
    - ``logger`` — logger name
    - ``msg``    — rendered log message

    Optional fields appended when present:

    - ``exc``    — formatted exception traceback
    - ``stack``  — formatted stack info
    - any ``extra=`` keys passed to the logger call
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        entry: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        # Merge any extra fields injected via `extra={...}` on the log call.
        for key, val in record.__dict__.items():
            if key not in _LOG_RECORD_BUILTIN_ATTRS and not key.startswith("_"):
                entry[key] = val
        return json.dumps(entry, default=str)


def configure_logging(
    log_file: "Path | str",
    *,
    file_level: int = logging.DEBUG,
    console_level: int = logging.INFO,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Configure structured JSON logging for the daemon.

    Sets up two handlers on the root logger:

    - **RotatingFileHandler** — writes JSON lines to *log_file*
      (rotates at *max_bytes*, keeps *backup_count* backups).
    - **StreamHandler** — writes human-readable lines to stderr
      at *console_level* (default ``INFO``).

    This function is idempotent: subsequent calls are no-ops.

    Args:
        log_file: Path to the daemon log file.
            The parent directory is created automatically.
        file_level: Minimum level for the file handler (default ``DEBUG``).
        console_level: Minimum level for the console handler (default ``INFO``).
        max_bytes: Maximum file size before rotation (default 10 MiB).
        backup_count: Number of rotated backup files to retain (default 5).
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_path = Path(log_file).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # handlers filter independently

    # Rotating JSON file handler
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(JsonFormatter())

    # Human-readable console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    )

    root.addHandler(file_handler)
    root.addHandler(console_handler)

    _CONFIGURED = True
