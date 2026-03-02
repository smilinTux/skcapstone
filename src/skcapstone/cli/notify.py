"""Notification commands: skcapstone notify test."""

from __future__ import annotations

import click

from ._common import console


def register_notify_commands(main: click.Group) -> None:
    """Register the notify command group."""

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
    def notify_test(title: str, body: str, urgency: str):
        """Send a test desktop notification."""
        from ..notifications import NotificationManager

        # Bypass debounce for the test command by using a fresh manager
        mgr = NotificationManager(debounce_seconds=0)
        dispatched = mgr.notify(title, body, urgency)
        if dispatched:
            console.print(f"\n  [green]Notification dispatched[/]: {title!r} / {body!r}\n")
        else:
            console.print(
                "\n  [yellow]Notification not dispatched[/] — no supported notification "
                "system found (notify-send / osascript).\n"
            )
