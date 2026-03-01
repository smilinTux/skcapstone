"""Input validation helpers for CLI commands.

Centralises sanitisation of user-supplied agent names, task IDs,
and file paths to guard against injection and traversal attacks.
"""

from __future__ import annotations

import re
import sys

import click


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?$")
_TASK_ID_RE = re.compile(r"^[0-9a-fA-F\-]+$")
_SOUL_NAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9\-_]*[a-zA-Z0-9])?$")


# ---------------------------------------------------------------------------
# Validators (raise click.BadParameter on failure)
# ---------------------------------------------------------------------------


def validate_agent_name(name: str) -> str:
    """Validate an agent name: alphanumeric + hyphens, 1-64 chars."""
    if not name or len(name) > 64:
        raise click.BadParameter(
            f"Agent name must be 1-64 characters, got {len(name) if name else 0}."
        )
    if not _AGENT_NAME_RE.match(name):
        raise click.BadParameter(
            f"Agent name '{name}' is invalid. "
            "Use only letters, digits, and hyphens (cannot start/end with hyphen)."
        )
    return name


def validate_task_id(task_id: str) -> str:
    """Validate a task ID: hex characters and hyphens only, 1-64 chars."""
    if not task_id or len(task_id) > 64:
        raise click.BadParameter(
            f"Task ID must be 1-64 characters, got {len(task_id) if task_id else 0}."
        )
    if not _TASK_ID_RE.match(task_id):
        raise click.BadParameter(
            f"Task ID '{task_id}' is invalid. Use only hex characters (0-9, a-f) and hyphens."
        )
    return task_id


def validate_soul_name(name: str) -> str:
    """Validate a soul name: alphanumeric + hyphens + underscores, 1-64 chars."""
    if not name or len(name) > 64:
        raise click.BadParameter(
            f"Soul name must be 1-64 characters, got {len(name) if name else 0}."
        )
    if not _SOUL_NAME_RE.match(name):
        raise click.BadParameter(
            f"Soul name '{name}' is invalid. "
            "Use only letters, digits, hyphens, and underscores."
        )
    return name


def validate_file_path(path: str) -> str:
    """Reject path traversal sequences in user-supplied file paths."""
    if ".." in path.split("/") or ".." in path.split("\\"):
        raise click.BadParameter(
            f"Path '{path}' contains '..' traversal sequences — rejected."
        )
    return path
