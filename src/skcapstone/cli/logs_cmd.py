"""Logs command — tail daemon logs with optional filtering."""

from __future__ import annotations

import re
import time
from collections import deque
from pathlib import Path

import click

from ._common import AGENT_HOME, console
from .. import SKCAPSTONE_ROOT

# Log level ordering (lowest → highest)
_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Rich markup style per level
_LEVEL_STYLE: dict[str, str] = {
    "DEBUG": "dim",
    "INFO": "",
    "WARNING": "yellow",
    "ERROR": "red",
    "CRITICAL": "bold red",
}

# Regex to extract level from daemon log format:
# "2026-03-02 12:34:56,789 [skcapstone.daemon] INFO: message"
_LEVEL_RE = re.compile(r"\]\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL):")


# ---------------------------------------------------------------------------
# Helpers (importable for unit-testing)
# ---------------------------------------------------------------------------

def _resolve_log_file(agent: str | None, home: str) -> Path:
    """Return the path to daemon.log for the given agent/home."""
    if agent:
        base = (Path(SKCAPSTONE_ROOT) / "agents" / agent).expanduser()
    else:
        base = Path(home).expanduser()
    return base / "logs" / "daemon.log"


def _parse_level(line: str) -> str | None:
    """Extract the log level keyword from a formatted log line, or None."""
    m = _LEVEL_RE.search(line)
    return m.group(1) if m else None


def _matches_filters(line: str, min_level: str | None, peer: str | None) -> bool:
    """Return True if *line* passes the active filters.

    Args:
        line: A single log line (no trailing newline).
        min_level: Minimum log level (e.g. ``"WARNING"``).  Lines whose level
            is below this threshold are excluded.  ``None`` disables the filter.
        peer: Substring to match against the line (case-insensitive).
            ``None`` disables the filter.
    """
    if min_level:
        lvl = _parse_level(line)
        if lvl is None:
            return False
        if _LEVELS.index(lvl) < _LEVELS.index(min_level):
            return False
    if peer and peer.lower() not in line.lower():
        return False
    return True


def _format_line(line: str) -> str:
    """Wrap *line* in Rich markup that reflects its log level."""
    lvl = _parse_level(line)
    style = _LEVEL_STYLE.get(lvl or "", "")
    if not style:
        return line
    return f"[{style}]{line}[/]"


def _tail(path: Path, n: int) -> list[str]:
    """Return the last *n* lines from *path* (with newlines preserved)."""
    with open(path) as fh:
        return list(deque(fh, maxlen=n))


# ---------------------------------------------------------------------------
# Command registration
# ---------------------------------------------------------------------------

def register_logs_commands(main: click.Group) -> None:
    """Register the top-level ``skcapstone logs`` command."""

    @main.command("logs")
    @click.option(
        "--home", default=AGENT_HOME, type=click.Path(),
        help="Agent home directory.",
    )
    @click.option(
        "--agent", default=None,
        help="Named agent whose logs to read (e.g. opus, jarvis).",
    )
    @click.option(
        "--follow", "-f", is_flag=True,
        help="Stream new log entries as they arrive (like tail -f).",
    )
    @click.option(
        "--lines", "-n", default=50, show_default=True,
        help="Number of recent lines to show.",
    )
    @click.option(
        "--level",
        default=None,
        type=click.Choice(
            ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            case_sensitive=False,
        ),
        help="Minimum log level to display.",
    )
    @click.option(
        "--peer", default=None,
        help="Only show lines that mention this peer name.",
    )
    def logs_command(
        home: str,
        agent: str | None,
        follow: bool,
        lines: int,
        level: str | None,
        peer: str | None,
    ) -> None:
        """Tail daemon logs in real-time.

        Reads from ~/.skcapstone/logs/daemon.log by default.
        Use --agent or --home to target a specific named agent.

        Examples:

            skcapstone logs

            skcapstone logs -n 100

            skcapstone logs -f

            skcapstone logs --level WARNING

            skcapstone logs --peer opus --follow
        """
        log_file = _resolve_log_file(agent, home)
        min_level = level.upper() if level else None

        if not log_file.exists():
            console.print(
                f"[yellow]Log file not found:[/] {log_file}\n"
                "[dim]Start the daemon to create logs.[/]"
            )
            return

        if not follow:
            # Static mode: read last N lines, apply filters, print, exit.
            raw = _tail(log_file, lines)
            filtered = [
                ln.rstrip("\n")
                for ln in raw
                if _matches_filters(ln.rstrip("\n"), min_level, peer)
            ]
            if not filtered:
                console.print("[dim]No matching log lines.[/]")
                return
            for ln in filtered:
                console.print(_format_line(ln))
            return

        # Follow mode: show initial N lines, then stream new content.
        try:
            with open(log_file) as fh:
                # Emit the last N historical lines first.
                initial = list(deque(fh, maxlen=lines))
                for ln in initial:
                    ln = ln.rstrip("\n")
                    if _matches_filters(ln, min_level, peer):
                        console.print(_format_line(ln))

                # Poll for new content until Ctrl-C.
                while True:
                    chunk = fh.read()
                    if chunk:
                        for ln in chunk.splitlines():
                            if _matches_filters(ln, min_level, peer):
                                console.print(_format_line(ln))
                    time.sleep(0.2)

        except KeyboardInterrupt:
            pass
