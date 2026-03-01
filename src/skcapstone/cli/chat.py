"""Agent-to-agent chat commands: send, inbox, live, open.

skcapstone chat <peer>          Open interactive session (shortcut)
skcapstone chat open <peer>     Open interactive prompt_toolkit session
skcapstone chat send <peer> <m> One-shot send
skcapstone chat inbox           Browse messages
skcapstone chat live <peer>     Alias for 'open'
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click

from ._common import AGENT_HOME, console, get_runtime
from ._validators import validate_agent_name

from rich.table import Table


# Known sub-command names; anything else is treated as a peer name.
_KNOWN_SUBCOMMANDS = {"send", "inbox", "live", "open", "--help", "-h", "--version"}


class _ChatGroup(click.Group):
    """Click group that treats an unknown first arg as a peer for 'open'.

    Allows::

        skcapstone chat lumina        # same as: skcapstone chat open lumina
        skcapstone chat send lumina … # normal subcommand routing
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and not args[0].startswith("-") and args[0] not in _KNOWN_SUBCOMMANDS:
            args = ["open"] + args
        return super().parse_args(ctx, args)


def register_chat_commands(main: click.Group) -> None:
    """Register the chat command group."""

    @main.group(cls=_ChatGroup)
    def chat():
        """Agent-to-agent chat — sovereign P2P messaging.

        Open an interactive session, send one-off messages, or browse
        your inbox. Works from any terminal — no IDE required.

        \b
        Quick start:
          skcapstone chat lumina           # start chatting with 'lumina'
          skcapstone chat send opus "hi"   # send a one-off message
          skcapstone chat inbox --poll     # check for new messages
        """

    # ------------------------------------------------------------------
    # open — interactive prompt_toolkit session
    # ------------------------------------------------------------------

    @chat.command("open")
    @click.argument("peer")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option(
        "--thread", "-t", default=None,
        help="Start in a specific thread ID.",
    )
    @click.option(
        "--poll-interval", default=2.0, show_default=True,
        help="Seconds between incoming message polls.",
    )
    def chat_open(peer: str, home: str, thread: Optional[str], poll_interval: float):
        """Open an interactive chat session with a peer.

        Uses prompt_toolkit for a rich terminal experience: command
        history, auto-suggest, slash-command completion, and a status
        bar. Falls back to a plain readline loop if prompt_toolkit is
        not installed.

        \b
        Slash commands inside the session:
          /attach <path>   Send a file attachment (max 10 MB)
          /thread <id>     Switch conversation thread
          /reply           Switch to the last received thread
          /inbox           Preview last 5 messages
          /whoami          Show your identity
          /emoji           Emoji quick reference
          /quit            Exit  (/exit or /q also work)

        Emoji is fully supported — just type directly: 🎉 🚀 ❤️

        \b
        Examples:
          skcapstone chat lumina
          skcapstone chat open lumina --thread deploy-v3
          skcapstone chat open opus --poll-interval 5
        """
        from ..chat import AgentChat

        validate_agent_name(peer)

        home_path = Path(home).expanduser()
        if not home_path.exists():
            console.print("[bold red]No agent found.[/] Run skcapstone init first.")
            sys.exit(1)

        runtime = get_runtime(home_path)
        identity = runtime.manifest.name or "unknown"

        agent_chat = AgentChat(home=home_path, identity=identity)
        agent_chat.interactive_session(peer, poll_interval=poll_interval, thread_id=thread)

    # ------------------------------------------------------------------
    # send — one-shot message
    # ------------------------------------------------------------------

    @chat.command("send")
    @click.argument("peer")
    @click.argument("message")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--thread", "-t", default=None, help="Thread ID for conversation grouping.")
    def chat_send(peer: str, message: str, home: str, thread: Optional[str]):
        """Send a message to a peer agent.

        Stores locally and delivers via SKComm if transports
        are configured.

        \b
        Examples:
          skcapstone chat send lumina "Hello from the sovereign side!"
          skcapstone chat send opus "Deploy update ready" --thread deploy-01
        """
        from ..chat import AgentChat

        validate_agent_name(peer)

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

    # ------------------------------------------------------------------
    # inbox — browse messages
    # ------------------------------------------------------------------

    @chat.command("inbox")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--limit", "-n", default=20, help="Max messages to show.")
    @click.option("--poll", is_flag=True, help="Poll transports for new messages first.")
    def chat_inbox(home: str, limit: int, poll: bool):
        """Show recent messages.

        Displays messages from local history. Use --poll to check
        SKComm transports for new messages first.

        \b
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

        from ..chat import _format_content

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
            content = _format_content(msg.get("content", ""))
            preview = content[:50] + ("…" if len(content) > 50 else "")
            ts = str(msg.get("timestamp", ""))
            if len(ts) > 19:
                ts = ts[:19]
            table.add_row(sender, preview, ts)

        console.print(table)
        console.print("")

    # ------------------------------------------------------------------
    # live — alias for open (backwards compat)
    # ------------------------------------------------------------------

    @chat.command("live")
    @click.argument("peer")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--poll-interval", default=2.0, help="Seconds between inbox polls (default: 2).")
    @click.option("--thread", "-t", default=None, help="Starting thread ID.")
    @click.pass_context
    def chat_live(ctx, peer: str, home: str, poll_interval: float, thread: Optional[str]):
        """Start a live interactive chat session with a peer.

        Alias for 'skcapstone chat open'. Uses prompt_toolkit when
        available, falls back to plain readline.

        \b
        Examples:
          skcapstone chat live lumina
          skcapstone chat live opus --poll-interval 5
        """
        ctx.invoke(chat_open, peer=peer, home=home, thread=thread, poll_interval=poll_interval)
