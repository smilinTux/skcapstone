"""Notification commands: skcapstone notify test / skcapstone notifications."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ._common import AGENT_HOME, console


def register_notify_commands(main: click.Group) -> None:
    """Register the notify command group and the notifications alias."""

    @main.group()
    def notify():
        """Desktop notification management."""

    @notify.command("test")
    @click.option("--title", default="SKCapstone", show_default=True, help="Notification title.")
    @click.option("--body", default="Test notification from SKCapstone.", show_default=True, help="Notification body.")
    @click.option(
        "--urgency",
        default="normal",
        show_default=True,
        type=click.Choice(["low", "normal", "critical"]),
        help="Notification urgency.",
    )
    @click.option(
        "--dashboard-url",
        default="http://localhost:7778",
        show_default=True,
        help="Dashboard URL opened when the 'Open Dashboard' action is clicked.",
    )
    def notify_test(title: str, body: str, urgency: str, dashboard_url: str):
        """Send a test desktop notification with click-action buttons."""
        from ..notifications import NotificationManager

        # Bypass debounce for the test command by using a fresh manager
        mgr = NotificationManager(debounce_seconds=0, dashboard_url=dashboard_url)
        dispatched = mgr.notify(title, body, urgency)
        if dispatched:
            console.print(f"\n  [green]Notification dispatched[/]: {title!r} / {body!r}\n")
            console.print(
                "  [dim]Click 'Open Dashboard' to open the dashboard, "
                "or 'Open SKChat' to launch skchat watch.[/]\n"
            )
        else:
            console.print(
                "\n  [yellow]Notification not dispatched[/] — no supported notification "
                "system found (gi.repository.Notify / notify-send / osascript).\n"
            )

    # -----------------------------------------------------------------------
    # Top-level alias: skcapstone notifications
    # -----------------------------------------------------------------------

    @main.command("notifications")
    @click.option("--home", default=AGENT_HOME, type=click.Path(), help="Agent home directory.")
    @click.option("--limit", "-n", default=20, show_default=True, help="Max results to show.")
    @click.option("--json-out", is_flag=True, help="Output as JSON.")
    def notifications_cmd(home: str, limit: int, json_out: bool):
        """Show notification history (memories tagged 'notification')."""
        import json as _json

        from ..memory_engine import search as mem_search

        home_path = Path(home).expanduser()
        if not home_path.exists():
            if json_out:
                print(_json.dumps([]))
                return
            console.print("[bold red]No agent found.[/] Run skcapstone init first.")
            sys.exit(1)

        results = mem_search(home=home_path, query="notification", tags=["notification"], limit=limit)

        if json_out:
            output = [
                {
                    "id": entry.memory_id,
                    "content": entry.content,
                    "tags": entry.tags,
                    "layer": entry.layer.value,
                    "created_at": entry.created_at.isoformat() if entry.created_at else None,
                }
                for entry in results
            ]
            print(_json.dumps(output))
            return

        if not results:
            console.print("\n  [dim]No notification history found.[/]\n")
            return

        from rich.table import Table

        console.print(f"\n  [bold]{len(results)}[/] notification{'s' if len(results) != 1 else ''} in history:\n")

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("ID", style="cyan", max_width=14)
        table.add_column("When", style="dim", max_width=22)
        table.add_column("Content", max_width=70)

        for entry in results:
            created = entry.created_at.strftime("%Y-%m-%d %H:%M:%S") if entry.created_at else "—"
            preview = entry.content[:100] + ("..." if len(entry.content) > 100 else "")
            table.add_row(entry.memory_id, created, preview)

        console.print(table)
        console.print()
