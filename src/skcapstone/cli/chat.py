"""Agent-to-agent chat commands: send, inbox, live, open, list, summary.

skcapstone chat <peer>              Open interactive LLM session (shortcut)
skcapstone chat open <peer>         Open interactive LLM session
skcapstone chat send <peer> <m>     One-shot send
skcapstone chat inbox               Browse messages
skcapstone chat live <peer>         Alias for 'open'
skcapstone chat list                List peers with conversation history
skcapstone chat --list              Same as 'list'
skcapstone chat summary <peer>      LLM-powered conversation summary
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Optional

import click

logger = logging.getLogger(__name__)

from ._common import AGENT_HOME, console, get_runtime
from ._validators import validate_agent_name

from rich.table import Table


# Known sub-command names; anything else is treated as a peer name.
_KNOWN_SUBCOMMANDS = {"send", "inbox", "live", "open", "list", "history", "summary", "forward", "--help", "-h", "--version"}


class _ChatGroup(click.Group):
    """Click group that treats an unknown first arg as a peer for 'open'.

    Allows::

        skcapstone chat lumina        # same as: skcapstone chat open lumina
        skcapstone chat send lumina … # normal subcommand routing
        skcapstone chat --list        # same as: skcapstone chat list
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if "--list" in args:
            remaining = [a for a in args if a != "--list"]
            args = ["list"] + remaining
        elif args and not args[0].startswith("-") and args[0] not in _KNOWN_SUBCOMMANDS:
            args = ["open"] + args
        return super().parse_args(ctx, args)


def _run_llm_chat(peer: str, home_path: Path, identity: str) -> None:
    """Run an LLM-powered interactive terminal chat session.

    Args:
        peer: Peer name used as conversation context key.
        home_path: Agent home directory.
        identity: Local agent name shown in the prompt.
    """
    from ..consciousness_loop import (
        ConsciousnessConfig,
        LLMBridge,
        SystemPromptBuilder,
        _classify_message,
    )

    config = ConsciousnessConfig()
    bridge = LLMBridge(config)
    builder = SystemPromptBuilder(home=home_path)

    # Show last 5 messages from existing history
    conv_file = home_path / "conversations" / f"{peer}.json"
    console.print()
    if conv_file.exists():
        try:
            history = json.loads(conv_file.read_text(encoding="utf-8"))
            if history:
                console.print(
                    f"[dim]--- {len(history)} previous message(s) with {peer} ---[/]\n"
                )
                for msg in history[-5:]:
                    if msg.get("role") == "user":
                        label = f"[cyan]{identity}[/]"
                    else:
                        label = f"[green]{peer}[/]"
                    content = msg.get("content", "")[:100]
                    console.print(f"  {label}: {content}")
                console.print()
        except Exception as exc:
            logger.warning("Failed to load previous conversation history with %s: %s", peer, exc)

    console.print(f"[bold]Chat with [cyan]{peer}[/][/]  [dim]Ctrl+C or /quit to exit[/]\n")

    try:
        while True:
            try:
                user_msg = console.input(f"[cyan]{identity}[/] > ").strip()
            except EOFError:
                break

            if not user_msg:
                continue
            if user_msg.lower() in ("/quit", "/exit", "/q"):
                break

            builder.add_to_history(peer, "user", user_msg)

            system_prompt = builder.build(peer_name=peer)
            signal = _classify_message(user_msg)

            with console.status("[dim]thinking...[/]"):
                try:
                    response = bridge.generate(system_prompt, user_msg, signal)
                except Exception as exc:
                    response = f"[Error: {exc}]"

            console.print(f"[green]{peer}[/]: {response}\n")
            builder.add_to_history(peer, "assistant", response)

    except KeyboardInterrupt:
        console.print("\n[dim]Session ended.[/]")


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
        """Open an interactive LLM-powered chat session.

        Starts a terminal chat loop that uses the local LLM (via
        LLMBridge) to generate responses. Conversation history is
        shown at startup and saved to conversations/{peer}.json.

        \b
        Slash commands:
          /quit  /exit  /q   — exit the session

        \b
        Examples:
          skcapstone chat lumina
          skcapstone chat open lumina
          skcapstone chat open opus
        """
        validate_agent_name(peer)

        home_path = Path(home).expanduser()
        if not home_path.exists():
            console.print("[bold red]No agent found.[/] Run skcapstone init first.")
            sys.exit(1)

        runtime = get_runtime(home_path)
        identity = runtime.manifest.name or "unknown"

        _run_llm_chat(peer, home_path, identity)

    # ------------------------------------------------------------------
    # send — one-shot message
    # ------------------------------------------------------------------

    @chat.command("send")
    @click.argument("peer")
    @click.argument("message")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--thread", "-t", default=None, help="Thread ID for conversation grouping.")
    @click.option("--encrypt", is_flag=True, default=False, help="Encrypt message with AES-256-GCM (key from KMS).")
    def chat_send(peer: str, message: str, home: str, thread: Optional[str], encrypt: bool):
        """Send a message to a peer agent.

        Stores locally and delivers via SKComm if transports
        are configured.

        \b
        Examples:
          skcapstone chat send lumina "Hello from the sovereign side!"
          skcapstone chat send opus "Deploy update ready" --thread deploy-01
          skcapstone chat send lumina "Secret plan" --encrypt
        """
        from ..chat import AgentChat

        validate_agent_name(peer)

        home_path = Path(home).expanduser()
        runtime = get_runtime(home_path)
        identity = runtime.manifest.name or "unknown"

        payload = message
        if encrypt:
            try:
                from ..message_crypto import encrypt_content
                payload = encrypt_content(message, home_path)
                console.print("  [dim]Message encrypted (AES-256-GCM)[/]")
            except Exception as exc:
                console.print(f"  [bold red]Encryption failed:[/] {exc}")
                sys.exit(1)

        agent_chat = AgentChat(home=home_path, identity=identity)
        result = agent_chat.send(peer, payload, thread_id=thread)

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
    @click.option("--decrypt", is_flag=True, default=False, help="Decrypt encrypted messages using KMS key.")
    def chat_inbox(home: str, limit: int, poll: bool, decrypt: bool):
        """Show recent messages.

        Displays messages from local history. Use --poll to check
        SKComm transports for new messages first. Use --decrypt to
        automatically decrypt AES-256-GCM encrypted messages.

        \b
        Examples:
          skcapstone chat inbox
          skcapstone chat inbox --poll --limit 5
          skcapstone chat inbox --decrypt
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
        from ..message_crypto import decrypt_content, is_encrypted_content

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
            raw_content = msg.get("content", "")
            if decrypt and is_encrypted_content(raw_content):
                try:
                    raw_content = decrypt_content(raw_content, home_path)
                except Exception as exc:
                    raw_content = f"[decrypt failed: {exc}]"
            content = _format_content(raw_content)
            preview = content[:50] + ("…" if len(content) > 50 else "")
            ts = str(msg.get("timestamp", ""))
            if len(ts) > 19:
                ts = ts[:19]
            table.add_row(sender, preview, ts)

        console.print(table)
        console.print("")

    # ------------------------------------------------------------------
    # list — show peers with conversation history
    # ------------------------------------------------------------------

    @chat.command("list")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def chat_list(home: str):
        """List all peers with conversation history.

        Shows each peer that has a saved conversation file, along with
        the message count and a preview of the most recent message.

        \b
        Examples:
          skcapstone chat list
          skcapstone chat --list
        """
        home_path = Path(home).expanduser()
        conversations_dir = home_path / "conversations"

        if not conversations_dir.exists():
            console.print("\n  [dim]No conversations yet.[/]\n")
            return

        conv_files = sorted(conversations_dir.glob("*.json"))
        if not conv_files:
            console.print("\n  [dim]No conversations yet.[/]\n")
            return

        table = Table(
            show_header=True,
            header_style="bold",
            box=None,
            padding=(0, 2),
            title=f"Conversations ({len(conv_files)} peer{'s' if len(conv_files) != 1 else ''})",
        )
        table.add_column("Peer", style="cyan")
        table.add_column("Messages", justify="right", style="dim")
        table.add_column("Last message", max_width=60)

        for conv_file in conv_files:
            peer = conv_file.stem
            try:
                data = json.loads(conv_file.read_text(encoding="utf-8"))
                count = str(len(data)) if isinstance(data, list) else "?"
                last = ""
                if isinstance(data, list) and data:
                    last = str(data[-1].get("content", ""))[:60]
                table.add_row(peer, count, last)
            except Exception:
                table.add_row(peer, "?", "[dim][corrupted][/]")

        console.print()
        console.print(table)
        console.print()

    # ------------------------------------------------------------------
    # history — full conversation transcript for a peer
    # ------------------------------------------------------------------

    @chat.command("history")
    @click.argument("peer")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--limit", "-n", default=0, help="Show last N messages (0 = all).")
    @click.option(
        "--json", "as_json", is_flag=True, default=False,
        help="Output raw JSON instead of a formatted table.",
    )
    def chat_history(peer: str, home: str, limit: int, as_json: bool):
        """Show the full conversation history with PEER.

        Reads the persisted conversation file and displays every message
        exchanged with the named peer, oldest first.  Use --limit to
        restrict to the most recent N messages.

        \b
        Examples:
          skcapstone chat history lumina
          skcapstone chat history opus --limit 10
          skcapstone chat history jarvis --json
        """
        from ..conversation_store import ConversationStore

        validate_agent_name(peer)

        home_path = Path(home).expanduser()
        store = ConversationStore(home_path)
        messages = store.load(peer)

        if not messages:
            console.print(f"\n  [dim]No conversation history with {peer}.[/]\n")
            return

        if limit > 0:
            messages = messages[-limit:]

        if as_json:
            import json as _json
            console.print(_json.dumps(messages, ensure_ascii=False, indent=2))
            return

        title = f"Conversation with {peer} ({len(messages)} message{'s' if len(messages) != 1 else ''})"
        table = Table(
            show_header=True,
            header_style="bold",
            box=None,
            padding=(0, 2),
            title=title,
        )
        table.add_column("Role", style="cyan", width=12)
        table.add_column("Message")
        table.add_column("Time", style="dim", width=20)

        for msg in messages:
            role = msg.get("role", "?")
            role_color = "green" if role == "assistant" else "cyan"
            content = msg.get("content", "")
            ts = str(msg.get("timestamp", ""))
            if len(ts) > 19:
                ts = ts[:19]
            table.add_row(
                f"[{role_color}]{role}[/]",
                content,
                ts,
            )

        console.print()
        console.print(table)
        console.print()

    # ------------------------------------------------------------------
    # forward — re-send a message to another peer
    # ------------------------------------------------------------------

    @chat.command("forward")
    @click.argument("peer")
    @click.argument("msg_id")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--thread", "-t", default=None, help="Thread ID for the forwarded message.")
    def chat_forward(peer: str, msg_id: str, home: str, thread: Optional[str]):
        """Forward a message to another peer agent.

        Looks up MSG_ID in the local inbox and forwards it to PEER,
        preserving the original sender and timestamp in the forward
        envelope. The forwarding agent is recorded as the new sender.

        \b
        Examples:
          skcapstone chat forward opus msg-abc123
          skcapstone chat forward lumina msg-xyz --thread fwd-thread-01
        """
        from ..chat import AgentChat

        validate_agent_name(peer)

        home_path = Path(home).expanduser()
        runtime = get_runtime(home_path)
        identity = runtime.manifest.name or "unknown"

        agent_chat = AgentChat(home=home_path, identity=identity)

        messages = agent_chat.get_inbox(limit=200)
        original = next(
            (m for m in messages if m.get("message_id") == msg_id),
            None,
        )

        if original is None:
            console.print(f"\n  [red]Message not found:[/] {msg_id}\n")
            sys.exit(1)

        result = agent_chat.forward(original, peer, thread_id=thread)

        console.print("")
        if result["delivered"]:
            console.print(
                f"  [green]Forwarded[/] to [cyan]{peer}[/] via {result['transport']}  "
                f"[dim](id: {result['forwarded_id']})[/]"
            )
        elif result["stored"]:
            console.print(
                f"  [yellow]Stored locally[/] for [cyan]{peer}[/]  "
                f"[dim](id: {result['forwarded_id']})[/]"
            )
            if result.get("error"):
                console.print(f"  [dim]{result['error']}[/]")
        else:
            console.print(f"  [red]Failed[/] — {result.get('error', 'unknown error')}")
        console.print("")

    # ------------------------------------------------------------------
    # summary — LLM-powered conversation summarizer
    # ------------------------------------------------------------------

    @chat.command("summary")
    @click.argument("peer")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option(
        "--last", "-n", default=20, show_default=True,
        help="Number of recent messages to include in the summary.",
    )
    @click.option(
        "--show-stored", is_flag=True, default=False,
        help="Show the previously stored summary instead of generating a new one.",
    )
    def chat_summary(peer: str, home: str, last: int, show_stored: bool):
        """Summarize a conversation with PEER using the LLM.

        Reads the last N messages with PEER, calls the local LLM to
        produce a 2-3 sentence summary, and stores it under
        ~/.skcapstone/summaries/{peer}.json for future reference.

        \b
        Examples:
          skcapstone chat summary lumina
          skcapstone chat summary opus --last 50
          skcapstone chat summary lumina --show-stored
        """
        from ..conversation_summarizer import ConversationSummarizer

        validate_agent_name(peer)
        home_path = Path(home).expanduser()

        summarizer = ConversationSummarizer(home=home_path)

        if show_stored:
            stored = summarizer.load_summary(peer)
            if stored is None:
                console.print(f"\n  [yellow]No stored summary for[/] [cyan]{peer}[/].\n")
                console.print("  Run without --show-stored to generate one.\n")
                return
            console.print(f"\n[bold]Stored summary for [cyan]{peer}[/][/]")
            console.print(f"[dim]{stored.generated_at[:19]}  ({stored.message_count} messages)[/]\n")
            console.print(stored.text)
            console.print()
            return

        console.print(f"\n[dim]Summarizing last {last} messages with {peer}...[/]")
        with console.status("[dim]calling LLM...[/]"):
            try:
                result = summarizer.summarize(peer, n=last)
            except ValueError as exc:
                console.print(f"\n  [red]Error:[/] {exc}\n")
                return

        console.print(f"\n[bold]Summary of conversation with [cyan]{peer}[/][/]")
        console.print(f"[dim]{result.generated_at[:19]}  ({result.message_count} messages summarized)[/]\n")
        console.print(result.text)
        console.print(f"\n[dim]Saved to: {home_path}/summaries/{peer}.json[/]\n")

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
