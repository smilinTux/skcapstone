"""Agent-to-agent chat commands: send, inbox, live."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click

from ._common import AGENT_HOME, console, get_runtime

from rich.table import Table


def register_chat_commands(main: click.Group) -> None:
    """Register the chat command group."""

    @main.group()
    def chat():
        """Agent-to-agent chat — sovereign P2P messaging.

        Send messages, check your inbox, or start a live
        interactive chat session with another agent. Works
        from any terminal on any platform.
        """

    @chat.command("send")
    @click.argument("peer")
    @click.argument("message")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--thread", "-t", default=None, help="Thread ID for conversation grouping.")
    def chat_send(peer: str, message: str, home: str, thread: Optional[str]):
        """Send a message to a peer agent.

        Stores locally and delivers via SKComm if transports
        are configured.

        Examples:

            skcapstone chat send lumina "Hello from the sovereign side!"

            skcapstone chat send opus "Deploy update ready" --thread deploy-01
        """
        from ..chat import AgentChat

        home_path = Path(home).expanduser()
        runtime = get_runtime(home_path)
        identity = runtime.manifest.name or "unknown"

        agent_chat = AgentChat(home=home_path, identity=identity)
        result = agent_chat.send(peer, message, thread_id=thread)

        console.print("")
        if result["delivered"]:
            console.print(f"  [green]Delivered[/] to [cyan]{peer}[/] via {result['transport']}")
        elif result["stored"]:
            console.print(f"  [yellow]Stored locally[/] for [cyan]{peer}[/]")
            if result.get("error"):
                console.print(f"  [dim]{result['error']}[/]")
        else:
            console.print(f"  [red]Failed[/] — {result.get('error', 'unknown error')}")
        console.print("")

    @chat.command("inbox")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--limit", "-n", default=20, help="Max messages to show.")
    @click.option("--poll", is_flag=True, help="Poll transports for new messages first.")
    def chat_inbox(home: str, limit: int, poll: bool):
        """Show recent messages.

        Displays messages from local history. Use --poll to check
        SKComm transports for new messages first.

        Examples:

            skcapstone chat inbox

            skcapstone chat inbox --poll --limit 5
        """
        from ..chat import AgentChat

        home_path = Path(home).expanduser()
        runtime = get_runtime(home_path)
        identity = runtime.manifest.name or "unknown"

        agent_chat = AgentChat(home=home_path, identity=identity)

        if poll:
            incoming = agent_chat.receive(limit=limit)
            if incoming:
                console.print(f"\n  [green]{len(incoming)} new message(s) received[/]\n")

        messages = agent_chat.get_inbox(limit=limit)

        console.print("")
        if not messages:
            console.print("  [dim]No messages.[/]")
            console.print("")
            return

        table = Table(
            show_header=True,
            header_style="bold",
            box=None,
            padding=(0, 2),
            title=f"Inbox ({len(messages)} message{'s' if len(messages) != 1 else ''})",
        )
        table.add_column("From", style="cyan", max_width=25)
        table.add_column("Content", max_width=50)
        table.add_column("Time", style="dim", max_width=20)

        for msg in messages:
            sender = msg.get("sender", "?")
            content = msg.get("content", "")
            preview = content[:50] + ("..." if len(content) > 50 else "")
            ts = str(msg.get("timestamp", ""))
            if len(ts) > 19:
                ts = ts[:19]
            table.add_row(sender, preview, ts)

        console.print(table)
        console.print("")

    @chat.command("live")
    @click.argument("peer")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--poll-interval", default=2.0, help="Seconds between inbox polls (default: 2).")
    def chat_live(peer: str, home: str, poll_interval: float):
        """Start a live interactive chat session with a peer.

        Opens a real-time terminal chat. Type messages and press
        Enter to send. Incoming messages appear automatically.
        Type /quit to exit.

        Works from any terminal — no IDE required.

        Examples:

            skcapstone chat live lumina

            skcapstone chat live opus --poll-interval 5
        """
        from ..chat import AgentChat

        home_path = Path(home).expanduser()
        if not home_path.exists():
            console.print("[bold red]No agent found.[/] Run skcapstone init first.")
            sys.exit(1)

        runtime = get_runtime(home_path)
        identity = runtime.manifest.name or "unknown"

        agent_chat = AgentChat(home=home_path, identity=identity)
        agent_chat.live_session(peer, poll_interval=poll_interval)
