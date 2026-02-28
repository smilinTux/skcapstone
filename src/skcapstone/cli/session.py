"""Session auto-capture commands: capture, stats."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ._common import AGENT_HOME, console


def register_session_commands(main: click.Group) -> None:
    """Register the session command group."""

    @main.group()
    def session():
        """Session auto-capture — the agent never forgets.

        Capture AI conversation content as sovereign memories.
        Works with any tool: pipe from Claude Code, paste from
        Cursor, or pass a transcript file. Key moments are
        auto-extracted, scored, and stored.
        """

    @session.command("capture")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--tag", "-t", multiple=True, help="Extra tags (repeatable).")
    @click.option("--source", "-s", default="session", help="Source identifier.")
    @click.option(
        "--min-importance",
        default=0.3,
        type=float,
        help="Minimum importance to store (0.0-1.0).",
    )
    @click.option("--file", "-f", type=click.Path(exists=True), help="Read from a file.")
    @click.option("--stdin", "use_stdin", is_flag=True, help="Read from stdin.")
    @click.argument("content", required=False)
    def session_capture(
        home: str,
        tag: tuple,
        source: str,
        min_importance: float,
        file: str | None,
        use_stdin: bool,
        content: str | None,
    ):
        """Capture conversation content as memories.

        Extracts key moments, auto-scores importance, deduplicates,
        and stores as searchable sovereign memories.

        Examples:

            skcapstone session capture "We decided to use Ed25519 for keys"

            skcapstone session capture --file transcript.txt

            echo "meeting notes here" | skcapstone session capture --stdin

            claude chat --print | skcapstone session capture --stdin -t claude-session
        """
        from ..session_capture import SessionCapture

        home_path = Path(home).expanduser()
        if not home_path.exists():
            console.print("[bold red]No agent found.[/] Run skcapstone init first.")
            sys.exit(1)

        if file:
            text = Path(file).read_text(encoding="utf-8")
        elif use_stdin:
            text = sys.stdin.read()
        elif content:
            text = content
        else:
            console.print("[red]Provide content as argument, --file, or --stdin.[/]")
            sys.exit(1)

        if not text.strip():
            console.print("[yellow]No content to capture.[/]")
            return

        cap = SessionCapture(home_path)
        entries = cap.capture(
            content=text,
            tags=list(tag),
            source=source,
            min_importance=min_importance,
        )

        if not entries:
            console.print("\n  [dim]No moments above importance threshold.[/]\n")
            return

        console.print(f"\n  [green]Captured {len(entries)} moment(s):[/]\n")
        for e in entries:
            preview = e.content[:80] + ("..." if len(e.content) > 80 else "")
            console.print(
                f"    [{e.layer.value}] imp={e.importance:.1f}  {preview}"
            )
            if e.tags:
                console.print(f"    [dim]tags: {', '.join(e.tags)}[/]")
        console.print()

    @session.command("stats")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def session_stats(home: str):
        """Show session capture statistics."""
        from ..memory_engine import search as mem_search

        home_path = Path(home).expanduser()
        results = mem_search(home_path, "session-capture", limit=500)
        captured = [r for r in results if "session-capture" in r.tags]

        if not captured:
            console.print("\n  [dim]No captured sessions yet.[/]\n")
            return

        console.print(f"\n  [bold]{len(captured)}[/] captured moment(s)\n")
        by_source: dict[str, int] = {}
        for m in captured:
            by_source[m.source] = by_source.get(m.source, 0) + 1

        for src, count in sorted(by_source.items(), key=lambda x: -x[1]):
            console.print(f"    {src}: {count}")
        console.print()
