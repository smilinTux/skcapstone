"""
SKCapstone Session Replayer — play back a recorded JSONL session.

Two modes:

``--dry-run`` (default in tests)
    Iterates entries and prints what *would* be called.  No handlers executed.

Live mode
    Calls the real MCP tool handlers directly (no MCP transport required).
    Useful for regression testing, debugging, and auditing.

Each replayed entry produces a ``ReplayResult``::

    ReplayResult(
        index=0,
        tool="memory_store",
        arguments={...},
        recorded_result=[...],   # what the original call returned
        replayed_result=[...],   # what the live replay returned (None in dry-run)
        duration_ms=12,
        match=True,              # True if text content matches (live only)
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generator, Optional

from .session_recorder import load_session

logger = logging.getLogger("skcapstone.session_replayer")


@dataclass
class ReplayResult:
    index: int
    tool: str
    arguments: dict[str, Any]
    recorded_result: list[Any]
    replayed_result: Optional[list[Any]]
    duration_ms: int
    match: Optional[bool]   # None in dry-run; True/False in live mode
    error: Optional[str] = None


class SessionReplayer:
    """Replay a recorded JSONL session file.

    Args:
        path:    Path to the ``.jsonl`` session file.
        dry_run: If *True*, skip actual handler invocation.
    """

    def __init__(self, path: Path, dry_run: bool = False) -> None:
        self._path = path
        self._dry_run = dry_run
        self._handlers: Optional[dict] = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def replay(self) -> Generator[ReplayResult, None, None]:
        """Yield a :class:`ReplayResult` for each recorded tool call."""
        entries = load_session(self._path)
        if not entries:
            logger.warning("Session file is empty: %s", self._path)
            return

        if not self._dry_run:
            self._handlers = _load_handlers()

        for idx, entry in enumerate(entries):
            yield self._replay_entry(idx, entry)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _replay_entry(self, idx: int, entry: dict[str, Any]) -> ReplayResult:
        tool = entry.get("tool", "<unknown>")
        arguments = entry.get("arguments", {})
        recorded = entry.get("result", [])
        orig_ms = entry.get("duration_ms", 0)

        if self._dry_run:
            return ReplayResult(
                index=idx,
                tool=tool,
                arguments=arguments,
                recorded_result=recorded,
                replayed_result=None,
                duration_ms=0,
                match=None,
            )

        # Live replay
        replayed: Optional[list[Any]] = None
        error: Optional[str] = None
        t0 = time.monotonic()
        try:
            replayed = _call_handler(self._handlers, tool, arguments)
        except Exception as exc:
            error = str(exc)
            logger.warning("Replay error on tool '%s': %s", tool, exc)
        elapsed = int((time.monotonic() - t0) * 1000)

        match: Optional[bool] = None
        if replayed is not None:
            match = _results_match(recorded, replayed)

        return ReplayResult(
            index=idx,
            tool=tool,
            arguments=arguments,
            recorded_result=recorded,
            replayed_result=replayed,
            duration_ms=elapsed,
            match=match,
            error=error,
        )


# ---------------------------------------------------------------------------
# Mock MCP server for dry-run
# ---------------------------------------------------------------------------

class MockMCPServer:
    """Minimal mock that accepts tool calls and returns recorded results.

    Used internally when you want to pipe replay output back through an
    MCP-compatible interface without running a real server.
    """

    def __init__(self, session_path: Path) -> None:
        self._entries = load_session(session_path)
        self._index = 0

    def call(self, tool: str, arguments: dict[str, Any]) -> Optional[list[Any]]:
        """Return the next recorded result that matches *tool*, or None."""
        for entry in self._entries[self._index:]:
            self._index += 1
            if entry.get("tool") == tool:
                return entry.get("result")
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_handlers() -> dict:
    """Import and return the live MCP handler table."""
    from .mcp_tools import collect_all_handlers
    return collect_all_handlers()


def _call_handler(handlers: Optional[dict], tool: str, arguments: dict) -> list[Any]:
    """Invoke a handler synchronously (wraps the async call)."""
    if not handlers:
        raise RuntimeError("Handler table not loaded")
    handler = handlers.get(tool)
    if handler is None:
        raise ValueError(f"No handler registered for tool '{tool}'")
    return asyncio.run(handler(arguments))


def _results_match(recorded: list[Any], replayed: list[Any]) -> bool:
    """Compare two result lists by their serialised text content."""
    def _texts(items: list[Any]) -> list[str]:
        texts = []
        for item in items:
            if isinstance(item, dict):
                texts.append(item.get("text", ""))
            elif hasattr(item, "text"):
                texts.append(item.text)
            else:
                texts.append(str(item))
        return texts

    return _texts(recorded) == _texts(replayed)
