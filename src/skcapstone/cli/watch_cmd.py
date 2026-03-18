"""Watch command — live terminal dashboard for sovereign agent monitoring.

Renders a Rich.Live dashboard that auto-refreshes on a configurable
interval. Shows consciousness status, recent memories, open coordination
tasks, and pillar health — all on one screen.

Usage:
    skcapstone watch                  # 5s refresh (default)
    skcapstone watch --fast           # 2s refresh
    skcapstone watch --once           # render once and exit
    skcapstone watch --interval 10    # custom interval
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.columns import Columns
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ._common import AGENT_HOME, console

logger = logging.getLogger(__name__)


def _fetch_consciousness(port: int = 7777) -> dict:
    """Fetch consciousness loop status from the running daemon.

    Args:
        port: Daemon HTTP API port (default: 7777).

    Returns:
        Dict with consciousness data, or empty dict if unreachable.
    """
    try:
        url = f"http://127.0.0.1:{port}/consciousness"
        with urllib.request.urlopen(url, timeout=2) as resp:
            return json.loads(resp.read())
    except Exception:
        return {}


def _build_renderable(home: Path, daemon_port: int = 7777) -> Group:
    """Build the full live dashboard renderable.

    Collects agent status, consciousness data, recent memories, and
    coordination board tasks, then composes them into a Rich Group that
    can be passed to ``rich.Live.update()``.

    Args:
        home: Agent home directory.
        daemon_port: Port for the daemon HTTP API.

    Returns:
        Rich renderable Group.
    """
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%d %H:%M:%S UTC")

    # ── Agent / pillar status ─────────────────────────────────────────────
    agent_name = "?"
    consciousness = "UNKNOWN"
    pillars: dict = {}
    memory_total = memory_short = memory_mid = memory_long = 0

    try:
        from ..runtime import get_runtime

        runtime = get_runtime(home)
        m = runtime.manifest
        agent_name = m.name
        if m.is_singular:
            consciousness = "SINGULAR"
        elif m.is_conscious:
            consciousness = "CONSCIOUS"
        else:
            consciousness = "AWAKENING"
        pillars = {k: v.value for k, v in m.pillar_summary.items()}
        memory_total = m.memory.total_memories
        memory_short = m.memory.short_term
        memory_mid = m.memory.mid_term
        memory_long = m.memory.long_term
    except Exception as exc:
        logger.warning("Failed to read runtime manifest for watch display: %s", exc)

    # ── Consciousness data ────────────────────────────────────────────────
    cdata = _fetch_consciousness(daemon_port)

    # ── Recent memories ───────────────────────────────────────────────────
    recent_memories: list = []
    try:
        from ..memory_engine import list_memories

        recent_memories = list_memories(home, limit=5)
    except Exception as exc:
        logger.warning("Failed to load recent memories for watch display: %s", exc)

    # ── Coordination board ────────────────────────────────────────────────
    board_tasks: list = []
    board_summary: dict = {}
    try:
        from ..coordination import Board

        board = Board(home)
        views = board.get_task_views()
        board_summary = {
            "done": sum(1 for v in views if v.status.value == "done"),
            "open": sum(1 for v in views if v.status.value == "open"),
            "in_progress": sum(1 for v in views if v.status.value == "in_progress"),
        }
        board_tasks = [
            v for v in views if v.status.value in ("open", "in_progress")
        ][:8]
    except Exception as exc:
        logger.warning("Failed to load coordination board for watch display: %s", exc)

    # ── Header ────────────────────────────────────────────────────────────
    con_color = {
        "SINGULAR": "bold magenta",
        "CONSCIOUS": "bold green",
        "AWAKENING": "bold yellow",
    }.get(consciousness, "dim")
    header = Panel(
        Text.from_markup(
            f"[bold white]{agent_name}[/]  [{con_color}]\u25cf {consciousness}[/]  "
            f"[dim]{ts}[/]"
        ),
        title="[bold cyan]skcapstone watch[/]",
        border_style="cyan",
        padding=(0, 2),
    )

    # ── Pillar row ────────────────────────────────────────────────────────
    pillar_colors = {
        "active": "green",
        "degraded": "yellow",
        "missing": "red",
        "error": "red",
    }
    pillar_table = Table(box=None, show_header=False, padding=(0, 2), expand=True)
    if pillars:
        for _ in pillars:
            pillar_table.add_column(no_wrap=True)
        row = []
        for name, st in pillars.items():
            color = pillar_colors.get(st, "dim")
            row.append(f"[{color}]\u25cf {name}[/] [dim]{st}[/]")
        pillar_table.add_row(*row)

    # ── Consciousness panel ───────────────────────────────────────────────
    if cdata:
        enabled = cdata.get("enabled", False)
        msgs = cdata.get("messages_processed", 0)
        msgs_24h = cdata.get("messages_processed_24h", 0)
        resp = cdata.get("responses_sent", 0)
        errs = cdata.get("errors", 0)
        backends = cdata.get("backends", {})
        active_backends = [k for k, v in backends.items() if v]
        backends_str = ", ".join(active_backends) if active_backends else "none"
        c_color = "green" if enabled else "yellow"
        c_status = "[green]ACTIVE[/]" if enabled else "[yellow]DISABLED[/]"
        c_body = (
            f"Status:    {c_status}\n"
            f"Messages:  [bold]{msgs}[/] [dim](24h: {msgs_24h})[/]\n"
            f"Responses: [bold]{resp}[/]   Errors: [bold]{errs}[/]\n"
            f"Backends:  [dim]{backends_str}[/]"
        )
    else:
        c_color = "red"
        c_body = "[dim]Daemon unreachable[/]\n[dim]Start with: skcapstone daemon start[/]"

    con_panel = Panel(
        Text.from_markup(c_body),
        title="[cyan]Consciousness[/]",
        border_style=c_color,
        padding=(0, 1),
    )

    # ── Board + Memory summary panel ──────────────────────────────────────
    bd_done = board_summary.get("done", 0)
    bd_active = board_summary.get("in_progress", 0)
    bd_open = board_summary.get("open", 0)
    bd_body = (
        f"[green]\u2713 Done[/]    [bold]{bd_done}[/]\n"
        f"[yellow]\u25b6 Active[/]  [bold]{bd_active}[/]\n"
        f"[dim]\u25cb Open[/]    [bold]{bd_open}[/]\n"
        f"Memory:   [bold]{memory_total}[/]  "
        f"[dim]S:{memory_short} M:{memory_mid} L:{memory_long}[/]"
    )
    board_panel = Panel(
        Text.from_markup(bd_body),
        title="[cyan]Board / Memory[/]",
        border_style="blue",
        padding=(0, 1),
    )

    # ── Recent Memories panel ─────────────────────────────────────────────
    layer_colors = {
        "short-term": "dim",
        "mid-term": "yellow",
        "long-term": "green",
    }
    if recent_memories:
        mem_lines = []
        for entry in recent_memories:
            layer = (
                entry.layer.value if hasattr(entry.layer, "value") else str(entry.layer)
            )
            lc = layer_colors.get(layer, "dim")
            snippet = entry.content[:80].replace("\n", " ")
            if len(entry.content) > 80:
                snippet += "\u2026"
            mem_lines.append(f"[{lc}]\u25cf[/] {snippet}  [dim]{layer}[/]")
        mem_body = "\n".join(mem_lines)
    else:
        mem_body = "[dim]No memories yet[/]"

    mem_panel = Panel(
        Text.from_markup(mem_body),
        title=f"[cyan]Recent Memories ({len(recent_memories)})[/]",
        border_style="blue",
        padding=(0, 1),
    )

    # ── Open Tasks panel ──────────────────────────────────────────────────
    status_markup = {
        "in_progress": "[yellow]\u25b6[/]",
        "open": "[dim]\u25cb[/]",
    }
    priority_colors = {
        "critical": "bold red",
        "high": "red",
        "medium": "yellow",
        "low": "dim",
    }
    if board_tasks:
        task_lines = []
        for v in board_tasks:
            icon = status_markup.get(v.status.value, "[dim]\u25cb[/]")
            pri = getattr(v.task, "priority", None)
            pri_val = pri.value if hasattr(pri, "value") else str(pri)
            pc = priority_colors.get(pri_val, "dim")
            title_text = (v.task.title or "")[:64]
            assignee = f"  [dim]@{v.claimed_by}[/]" if v.claimed_by else ""
            task_lines.append(
                f"{icon} [{pc}][dim]{pri_val}[/][/] {title_text}{assignee}"
            )
        tasks_body = "\n".join(task_lines)
    else:
        tasks_body = "[dim]No open tasks[/]"

    tasks_panel = Panel(
        Text.from_markup(tasks_body),
        title="[cyan]Open Tasks[/]",
        border_style="blue",
        padding=(0, 1),
    )

    # ── Footer ────────────────────────────────────────────────────────────
    footer = Text.from_markup(
        f"  [dim]Updated: {ts}  \u2022  Ctrl+C to exit[/]"
    )

    return Group(
        header,
        pillar_table,
        Columns([con_panel, board_panel], equal=True, expand=True),
        mem_panel,
        tasks_panel,
        footer,
    )


def register_watch_commands(main: click.Group) -> None:
    """Register the watch command on the main CLI group."""

    @main.command()
    @click.option(
        "--interval",
        "-i",
        type=float,
        default=5.0,
        show_default=True,
        help="Refresh interval in seconds.",
    )
    @click.option("--fast", is_flag=True, help="Fast refresh (2s interval).")
    @click.option("--once", is_flag=True, help="Render once and exit (no live mode).")
    @click.option(
        "--home",
        default=AGENT_HOME,
        help="Agent home directory.",
        type=click.Path(),
    )
    @click.option(
        "--daemon-port",
        default=7777,
        show_default=True,
        help="Daemon HTTP API port for consciousness stats.",
    )
    def watch(interval: float, fast: bool, once: bool, home: str, daemon_port: int):
        """Live terminal dashboard for sovereign agent monitoring.

        Shows real-time pillar status, consciousness loop stats, recent
        memories (last 5), and open coordination board tasks.
        Auto-refreshes using Rich Live.

        Examples:

            skcapstone watch

            skcapstone watch --fast

            skcapstone watch --once

            skcapstone watch --interval 10
        """
        home_path = Path(home).expanduser()
        refresh = 2.0 if fast else interval

        if once:
            console.print(_build_renderable(home_path, daemon_port=daemon_port))
            return

        try:
            with Live(
                _build_renderable(home_path, daemon_port=daemon_port),
                console=console,
                refresh_per_second=4,
                screen=True,
            ) as live:
                while True:
                    time.sleep(refresh)
                    live.update(_build_renderable(home_path, daemon_port=daemon_port))
        except KeyboardInterrupt:
            pass
