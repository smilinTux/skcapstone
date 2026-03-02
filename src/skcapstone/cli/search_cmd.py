"""Search command: full-text search across memories, conversations, and messages."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from rich.table import Table
from rich.text import Text

from ._common import AGENT_HOME, console


# Source icons shown in the table
_SOURCE_ICON = {
    "memory": "🧠",
    "conversation": "💬",
    "message": "📨",
    "journal": "📓",
}

_SOURCE_COLOR = {
    "memory": "cyan",
    "conversation": "green",
    "message": "yellow",
    "journal": "magenta",
}

_VALID_SOURCES = ("memory", "conversation", "message", "journal")


def _fmt_ts(ts) -> str:
    """Format a datetime for display, returning '--' for None."""
    if ts is None:
        return "--"
    return ts.strftime("%Y-%m-%d %H:%M")


def register_search_commands(main: click.Group) -> None:
    """Register the top-level ``search`` command."""

    @main.command("search")
    @click.argument("query")
    @click.option(
        "--home",
        default=AGENT_HOME,
        type=click.Path(),
        help="Agent home directory.",
    )
    @click.option(
        "--type", "-t",
        "source_types",
        multiple=True,
        type=click.Choice(_VALID_SOURCES),
        help=(
            "Restrict search to a specific source type. "
            "May be repeated: -t memory -t conversation"
        ),
    )
    @click.option(
        "--limit", "-n",
        default=20,
        show_default=True,
        help="Maximum number of results.",
    )
    @click.option(
        "--json-out",
        is_flag=True,
        help="Output results as JSON.",
    )
    def search_cmd(query, home, source_types, limit, json_out):
        """Search across memories, conversations, and messages.

        QUERY is matched case-insensitively against all data stores.
        Results are ranked by relevance and recency.

        Examples:\n
          skcapstone search "consciousness"\n
          skcapstone search "Opus" --type conversation\n
          skcapstone search "trust" -t memory -t journal -n 10\n
          skcapstone search "sprint" --json-out | jq .[].preview
        """
        from ..unified_search import search as unified_search, SOURCE_ALL

        home_path = Path(home).expanduser()
        if not home_path.exists():
            if json_out:
                print(json.dumps([]))
                return
            console.print(
                "[bold red]No agent found.[/] Run [cyan]skcapstone init[/] first."
            )
            sys.exit(1)

        sources = frozenset(source_types) if source_types else SOURCE_ALL

        results = unified_search(home=home_path, query=query, sources=sources, limit=limit)

        if json_out:
            output = [
                {
                    "source": r.source,
                    "id": r.result_id,
                    "title": r.title,
                    "preview": r.preview,
                    "score": round(r.score, 4),
                    "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                    "metadata": r.metadata,
                }
                for r in results
            ]
            print(json.dumps(output, indent=2))
            return

        if not results:
            console.print(
                f"\n  [dim]No results for '[/]{query}[dim]'[/] "
                f"across {', '.join(sorted(sources))}.\n"
            )
            return

        source_label = (
            ", ".join(sorted(sources))
            if len(sources) < len(SOURCE_ALL)
            else "all sources"
        )
        console.print(
            f"\n  [bold]{len(results)}[/] result"
            f"{'s' if len(results) != 1 else ''} for "
            f"[bold cyan]'{query}'[/] in {source_label}:\n"
        )

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Type", max_width=14)
        table.add_column("Title", max_width=30)
        table.add_column("Preview", max_width=55)
        table.add_column("Score", justify="right", max_width=7)
        table.add_column("Date", justify="right", style="dim", max_width=16)

        for r in results:
            icon = _SOURCE_ICON.get(r.source, "?")
            color = _SOURCE_COLOR.get(r.source, "white")
            source_text = Text(f"{icon} {r.source}", style=color)
            preview_clipped = r.preview[:110] + ("…" if len(r.preview) > 110 else "")
            table.add_row(
                source_text,
                r.title,
                preview_clipped,
                f"{r.score:.2f}",
                _fmt_ts(r.timestamp),
            )

        console.print(table)
        console.print()
