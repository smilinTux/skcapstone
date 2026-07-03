"""Journal commands: weekly LLM summary of the session journal."""

from __future__ import annotations

import click

from ._common import console


def register_journal_commands(main: click.Group) -> None:
    """Register the ``skcapstone journal`` command group."""

    @main.group("journal")
    def journal() -> None:
        """Work with the append-only session journal."""

    @journal.command("summary")
    @click.option(
        "--week", "window", flag_value=7, default=True,
        help="Summarize the last 7 days (default).",
    )
    @click.option(
        "--days", "window", type=int,
        help="Summarize the last N days instead of a week.",
    )
    @click.option("--json-out", is_flag=True, help="Output raw JSON.")
    def journal_summary(window: int, json_out: bool) -> None:
        """Summarize recent journal entries with the agent's LLM.

        Gathers the last N days of journal entries (7 by default) and
        produces a concise, LLM-generated recap of the week's themes,
        notable moments, and emotional arc.

        \b
            skcapstone journal summary --week
            skcapstone journal summary --days 14
        """
        from ..journal_summary import summarize_week

        days = int(window) if window else 7
        try:
            result = summarize_week(days=days)
        except Exception as exc:  # pragma: no cover - defensive
            console.print(f"[red]Failed to summarize journal: {exc}[/]")
            raise SystemExit(1)

        if json_out:
            import json

            click.echo(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return

        _print_summary(result)


def _print_summary(result) -> None:
    """Render a :class:`WeeklyJournalSummary` with Rich."""
    from rich.panel import Panel

    if result.entry_count == 0:
        console.print()
        console.print(
            Panel(
                f"[dim]{result.text}[/]",
                title=f"[bold]Journal — last {result.window_days} days[/]",
                border_style="dim",
            )
        )
        console.print()
        return

    header = (
        f"[dim]{result.entry_count} "
        f"{'entry' if result.entry_count == 1 else 'entries'} · "
        f"{result.since[:10]} → {result.until[:10]}[/]\n\n"
        f"{result.text}"
    )
    console.print()
    console.print(
        Panel(
            header,
            title=f"[bold]Journal Summary — last {result.window_days} days[/]",
            border_style="cyan",
        )
    )
    console.print()
