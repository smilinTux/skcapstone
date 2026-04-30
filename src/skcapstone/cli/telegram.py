"""Telegram integration CLI commands."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import click

from ._common import AGENT_HOME, console

from rich.panel import Panel
from rich.table import Table


def register_telegram_commands(main: click.Group) -> None:
    """Register the telegram command group."""

    @main.group()
    def telegram():
        """Telegram integration — send, poll, list chats, check setup.

        Requires TELEGRAM_API_ID and TELEGRAM_API_HASH environment variables.
        Install Telethon with: pip install skmemory[telegram]
        """

    @telegram.command("setup")
    def telegram_setup():
        """Check Telegram API setup status.

        Reports whether Telethon is installed, API credentials are set,
        and a session file exists.
        """
        try:
            from skmemory.importers.telegram_api import check_setup
        except ImportError:
            console.print("[red]skmemory not available.[/] Install with: pip install skmemory[telegram]")
            raise SystemExit(1)

        result = check_setup()

        status_icon = "[green]READY[/]" if result["ready"] else "[red]NOT READY[/]"
        lines = [f"Status: {status_icon}"]
        lines.append(f"Telethon installed: {'[green]yes[/]' if result['telethon'] else '[red]no[/]'}")
        lines.append(f"Credentials set:    {'[green]yes[/]' if result['credentials'] else '[red]no[/]'}")
        lines.append(f"Session file:       {'[green]yes[/]' if result['session'] else '[yellow]no (first run will prompt)[/]'}")

        if result["messages"]:
            lines.append("")
            lines.append("[bold]Action items:[/]")
            for msg in result["messages"]:
                lines.append(f"  [yellow]>[/] {msg}")

        console.print(Panel("\n".join(lines), title="Telegram Setup", border_style="cyan"))

    @telegram.command("send")
    @click.argument("chat")
    @click.argument("message")
    @click.option("--parse-mode", "-p", type=click.Choice(["html", "markdown"]),
                  help="Message parse mode.")
    def telegram_send(chat, message, parse_mode):
        """Send a message to a Telegram chat.

        Example: skcapstone telegram send @username "Hello there!"
        """
        try:
            from skmemory.importers.telegram_api import send_message
        except ImportError:
            console.print("[red]skmemory[telegram] not available.[/] Install with: pip install skmemory[telegram]")
            raise SystemExit(1)

        try:
            result = asyncio.run(send_message(chat, message, parse_mode))
            console.print(Panel(
                f"[green]Sent![/]\n"
                f"Chat: [cyan]{result['chat']}[/]\n"
                f"Message ID: [dim]{result['message_id']}[/]\n"
                f"Date: {result['date']}",
                title="Telegram Send",
                border_style="green",
            ))
        except Exception as e:
            console.print(f"[red]Error:[/] {e}")
            raise SystemExit(1)

    @telegram.command("poll")
    @click.argument("chat")
    @click.option("--limit", "-l", default=20, type=int, help="Max messages to fetch.")
    @click.option("--since", "-s", default=None, help="Only messages after this date (YYYY-MM-DD).")
    def telegram_poll(chat, limit, since):
        """Fetch recent messages from a Telegram chat.

        Example: skcapstone telegram poll @channel --limit 10
        """
        try:
            from skmemory.importers.telegram_api import poll_messages
        except ImportError:
            console.print("[red]skmemory[telegram] not available.[/] Install with: pip install skmemory[telegram]")
            raise SystemExit(1)

        try:
            messages = asyncio.run(poll_messages(chat, limit=limit, since=since))

            if not messages:
                console.print(f"[dim]No messages found in {chat}.[/]")
                return

            table = Table(title=f"Messages from {chat} ({len(messages)} shown)")
            table.add_column("ID", style="dim", width=10)
            table.add_column("Date", width=20)
            table.add_column("Sender", style="cyan", width=20)
            table.add_column("Text", no_wrap=False)

            for msg in messages:
                text = msg["text"][:120] + ("..." if len(msg["text"]) > 120 else "")
                table.add_row(
                    str(msg["id"]),
                    msg["date"][:19] if msg["date"] else "",
                    msg["sender"],
                    text,
                )

            console.print(table)
        except Exception as e:
            console.print(f"[red]Error:[/] {e}")
            raise SystemExit(1)

    @telegram.command("catchup")
    @click.argument("chat")
    @click.option("--limit", "-l", default=2000, type=int, help="Max messages to fetch.")
    @click.option("--since", "-s", default=None, help="Only messages after this date (YYYY-MM-DD).")
    @click.option("--min-length", "-m", default=20, type=int, help="Skip messages shorter than this.")
    @click.option("--tags", "-t", default=None, help="Extra comma-separated tags.")
    def telegram_catchup(chat, limit, since, min_length, tags):
        """Full catch-up import from a Telegram group into all memory tiers.

        Downloads chat via Telethon and distributes messages by age:
        last 24h → short-term, last 7 days → mid-term, older → long-term.

        Example: skcapstone telegram catchup @mygroup --limit 500
        """
        try:
            from skmemory.importers.telegram_api import import_telegram_api
            from skmemory.store import MemoryStore
        except ImportError:
            console.print("[red]skmemory[telegram] not available.[/] Install with: pip install skmemory[telegram]")
            raise SystemExit(1)

        try:
            tags_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
            store = MemoryStore()
            stats = import_telegram_api(
                store,
                chat,
                mode="catchup",
                limit=limit,
                since=since,
                min_message_length=min_length,
                tags=tags_list,
            )

            lines = [f"[green]Catch-up complete![/]"]
            for key, val in stats.items():
                lines.append(f"  {key}: [cyan]{val}[/]")

            console.print(Panel("\n".join(lines), title="Telegram Catch-Up", border_style="green"))

            # Post-import hook: run auto-tagger on the last 6 minutes of files
            # (just enough to cover what the catchup brought in).  Fires
            # inline so failures are visible; the hourly cron is the safety net.
            _auto_tag_script = os.path.expanduser(
                "~/.skcapstone/agents/lumina/scripts/auto-tag-hallucinations.py"
            )
            if os.path.isfile(_auto_tag_script):
                import subprocess as _subprocess
                _tag_result = _subprocess.run(
                    [sys.executable, _auto_tag_script, "--hours", "0.1", "--quiet"],
                    capture_output=True,
                    text=True,
                )
                if _tag_result.returncode != 0:
                    console.print(
                        f"[yellow]auto-tag-hallucinations warning:[/] {_tag_result.stderr.strip() or 'non-zero exit'}"
                    )
                elif _tag_result.stdout.strip():
                    console.print(f"[dim]auto-tag:[/] {_tag_result.stdout.strip()}")
        except Exception as e:
            console.print(f"[red]Error:[/] {e}")
            raise SystemExit(1)

    @telegram.command("chats")
    @click.option("--limit", "-l", default=50, type=int, help="Max chats to list.")
    def telegram_chats(limit):
        """List available Telegram chats, groups, and channels.

        Example: skcapstone telegram chats --limit 20
        """
        try:
            from skmemory.importers.telegram_api import list_chats
        except ImportError:
            console.print("[red]skmemory[telegram] not available.[/] Install with: pip install skmemory[telegram]")
            raise SystemExit(1)

        try:
            chats = asyncio.run(list_chats(limit=limit))

            if not chats:
                console.print("[dim]No chats found.[/]")
                return

            table = Table(title=f"Telegram Chats ({len(chats)} shown)")
            table.add_column("ID", style="dim", width=14)
            table.add_column("Title", style="cyan", no_wrap=False)
            table.add_column("Type", width=12)
            table.add_column("Unread", justify="right", width=8)
            table.add_column("Username", style="dim")

            for c in chats:
                table.add_row(
                    str(c["id"]),
                    c["title"],
                    c["type"],
                    str(c["unread_count"]),
                    c.get("username") or "",
                )

            console.print(table)
        except Exception as e:
            console.print(f"[red]Error:[/] {e}")
            raise SystemExit(1)

    @telegram.command("soul-swap")
    @click.argument("chat")
    @click.option("--from", "from_soul", required=True, help="Current soul name to swap from.")
    @click.option("--to", "to_soul", required=True, help="New soul name to swap to.")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def telegram_soul_swap(chat, from_soul, to_soul, home):
        """Perform a soul swap and announce it to a Telegram chat.

        Swaps the active soul using the soul_switch module, then sends
        a notification message to the specified Telegram chat.

        Example: skcapstone telegram soul-swap @mychat --from base --to lumina
        """
        from ..soul_switch import set_active_switch

        home_path = Path(home).expanduser()

        # Perform the soul swap
        try:
            blueprint = set_active_switch(home_path, to_soul)
            display = blueprint.effective_agent_name()
        except FileNotFoundError as e:
            console.print(f"[red]Soul swap failed:[/] {e}")
            raise SystemExit(1)
        except ValueError as e:
            console.print(f"[red]Soul swap failed:[/] {e}")
            raise SystemExit(1)

        # Send notification to Telegram
        try:
            from skmemory.importers.telegram_api import send_message
        except ImportError:
            console.print(f"[green]Soul swapped:[/] {from_soul} -> {to_soul}")
            console.print("[red]skmemory[telegram] not available.[/] Swap succeeded but notification not sent.")
            raise SystemExit(1)

        message = f"Soul swap: {from_soul} -> {to_soul} ({display})"
        try:
            result = asyncio.run(send_message(chat, message))
            console.print(Panel(
                f"[green]Soul swapped and announced![/]\n"
                f"From: [yellow]{from_soul}[/]\n"
                f"To: [bold cyan]{to_soul}[/] ({display})\n"
                f"Chat: [cyan]{result['chat']}[/]\n"
                f"Message ID: [dim]{result['message_id']}[/]",
                title="Telegram Soul Swap",
                border_style="green",
            ))
        except Exception as e:
            console.print(f"[green]Soul swapped:[/] {from_soul} -> {to_soul}")
            console.print(f"[red]Telegram notification failed:[/] {e}")
            raise SystemExit(1)
