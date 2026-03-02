"""Error queue commands: list, retry, clear."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click
from rich.table import Table
from rich.text import Text

from ._common import console


def register_errors_commands(main: click.Group) -> None:
    """Register the ``errors`` command group."""

    @main.group("errors")
    def errors_grp():
        """Error recovery queue — inspect and replay failed operations.

        Failed LLM calls, message sends, and sync operations land here
        and are retried automatically with exponential backoff (max 3 times).
        """

    # ------------------------------------------------------------------
    # errors list
    # ------------------------------------------------------------------

    @errors_grp.command("list")
    @click.option(
        "--path",
        default=None,
        type=click.Path(),
        help="Override error queue JSON path.",
    )
    @click.option(
        "--all", "show_all",
        is_flag=True,
        help="Include resolved entries.",
    )
    @click.option(
        "--status",
        type=click.Choice(["pending", "retrying", "exhausted", "resolved"]),
        default=None,
        help="Filter by status.",
    )
    def errors_list(path, show_all, status):
        """List queued error entries."""
        from ..error_queue import ErrorQueue

        q = ErrorQueue(path=Path(path) if path else None)
        entries = q.list_entries(status=status, include_resolved=show_all)

        stats = q.stats()
        console.print(
            f"\n  Error queue — "
            f"[yellow]{stats.get('pending', 0)}[/] pending  "
            f"[red]{stats.get('exhausted', 0)}[/] exhausted  "
            f"[green]{stats.get('resolved', 0)}[/] resolved  "
            f"([dim]{stats.get('total', 0)} total[/])\n"
        )

        if not entries:
            console.print("  [dim]Queue is empty.[/]\n")
            return

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("ID", style="cyan", max_width=12)
        table.add_column("Type")
        table.add_column("Error", max_width=40)
        table.add_column("Retries", justify="right")
        table.add_column("Status")
        table.add_column("Next retry", style="dim")

        status_styles = {
            "pending": "yellow",
            "retrying": "blue",
            "exhausted": "red",
            "resolved": "green",
        }

        for entry in entries:
            color = status_styles.get(entry.status, "dim")
            table.add_row(
                entry.entry_id[:10],
                entry.operation_type,
                entry.error_message[:60] + ("…" if len(entry.error_message) > 60 else ""),
                str(entry.retry_count),
                Text(entry.status, style=color),
                entry.next_retry_at[:19].replace("T", " ") if entry.next_retry_at else "—",
            )

        console.print(table)
        console.print()

    # ------------------------------------------------------------------
    # errors retry
    # ------------------------------------------------------------------

    @errors_grp.command("retry")
    @click.argument("entry_id", required=False, default=None)
    @click.option(
        "--all", "retry_all",
        is_flag=True,
        help="Retry all due entries.",
    )
    @click.option(
        "--path",
        default=None,
        type=click.Path(),
        help="Override error queue JSON path.",
    )
    def errors_retry(entry_id, retry_all, path):
        """Retry a specific entry or all due entries.

        ENTRY_ID  Hex entry ID (or prefix) to retry.

        Without --all, ENTRY_ID is required.
        """
        from ..error_queue import ErrorQueue

        if not retry_all and not entry_id:
            console.print("[red]Provide ENTRY_ID or use --all.[/]")
            sys.exit(1)

        q = ErrorQueue(path=Path(path) if path else None)

        if retry_all:
            results = q.retry_all_due()
            if not results:
                console.print("\n  [dim]No entries currently due for retry.[/]\n")
                return
            ok = sum(1 for v in results.values() if v)
            fail = len(results) - ok
            console.print(
                f"\n  Retried [bold]{len(results)}[/] entries — "
                f"[green]{ok} resolved[/]  [yellow]{fail} still pending[/]\n"
            )
            return

        # Single entry: allow prefix matching
        entries = q.list_entries(include_resolved=True)
        matched = [e for e in entries if e.entry_id.startswith(entry_id)]
        if not matched:
            console.print(f"[red]No entry found matching '[/]{entry_id}[red]'.[/]")
            sys.exit(1)
        if len(matched) > 1:
            console.print(
                f"[yellow]Ambiguous prefix '[/]{entry_id}[yellow]' — "
                f"matches {len(matched)} entries. Use more characters.[/]"
            )
            sys.exit(1)

        success = q.retry(matched[0].entry_id)
        if success:
            console.print(f"\n  [green]Resolved:[/] {matched[0].entry_id[:10]}\n")
        else:
            console.print(
                f"\n  [yellow]Retry recorded for:[/] {matched[0].entry_id[:10]} "
                f"(retries={matched[0].retry_count})\n"
            )

    # ------------------------------------------------------------------
    # errors clear
    # ------------------------------------------------------------------

    @errors_grp.command("clear")
    @click.argument("entry_id", required=False, default=None)
    @click.option(
        "--all", "clear_all",
        is_flag=True,
        help="Clear all entries.",
    )
    @click.option(
        "--exhausted",
        is_flag=True,
        help="Clear only exhausted entries.",
    )
    @click.option(
        "--resolved",
        is_flag=True,
        help="Clear only resolved entries.",
    )
    @click.option(
        "--path",
        default=None,
        type=click.Path(),
        help="Override error queue JSON path.",
    )
    @click.option("--force", is_flag=True, help="Skip confirmation prompt.")
    def errors_clear(entry_id, clear_all, exhausted, resolved, path, force):
        """Remove entries from the queue.

        ENTRY_ID  Hex entry ID (or prefix) to remove.

        Use --all, --exhausted, or --resolved for bulk removal.
        """
        from ..error_queue import ErrorQueue, ErrorStatus

        if not any([entry_id, clear_all, exhausted, resolved]):
            console.print(
                "[red]Provide ENTRY_ID, --all, --exhausted, or --resolved.[/]"
            )
            sys.exit(1)

        q = ErrorQueue(path=Path(path) if path else None)

        if exhausted:
            if not force and not click.confirm("Clear all exhausted entries?"):
                console.print("[yellow]Aborted.[/]")
                return
            removed = q.clear_all(status=ErrorStatus.EXHAUSTED)
            console.print(f"\n  [red]Cleared[/] {removed} exhausted entr{'y' if removed == 1 else 'ies'}.\n")
            return

        if resolved:
            if not force and not click.confirm("Clear all resolved entries?"):
                console.print("[yellow]Aborted.[/]")
                return
            removed = q.clear_all(status=ErrorStatus.RESOLVED)
            console.print(f"\n  [green]Cleared[/] {removed} resolved entr{'y' if removed == 1 else 'ies'}.\n")
            return

        if clear_all:
            stats = q.stats()
            total = stats.get("total", 0)
            if not force and not click.confirm(f"Clear all {total} entries?"):
                console.print("[yellow]Aborted.[/]")
                return
            removed = q.clear_all()
            console.print(f"\n  Cleared [bold]{removed}[/] entr{'y' if removed == 1 else 'ies'}.\n")
            return

        # Single entry
        entries = q.list_entries(include_resolved=True)
        matched = [e for e in entries if e.entry_id.startswith(entry_id)]
        if not matched:
            console.print(f"[red]No entry found matching '[/]{entry_id}[red]'.[/]")
            sys.exit(1)
        if len(matched) > 1:
            console.print(
                f"[yellow]Ambiguous prefix '[/]{entry_id}[yellow]' — "
                f"matches {len(matched)} entries.[/]"
            )
            sys.exit(1)

        if not force and not click.confirm(f"Remove entry {matched[0].entry_id[:10]}?"):
            console.print("[yellow]Aborted.[/]")
            return

        q.remove(matched[0].entry_id)
        console.print(f"\n  [red]Removed:[/] {matched[0].entry_id[:10]}\n")

    # ------------------------------------------------------------------
    # errors stats
    # ------------------------------------------------------------------

    @errors_grp.command("stats")
    @click.option(
        "--path",
        default=None,
        type=click.Path(),
        help="Override error queue JSON path.",
    )
    def errors_stats(path):
        """Show error queue statistics."""
        from ..error_queue import ErrorQueue
        from rich.panel import Panel

        q = ErrorQueue(path=Path(path) if path else None)
        s = q.stats()

        console.print()
        console.print(Panel(
            f"[bold]Total:[/]     {s.get('total', 0)}\n"
            f"[yellow]Pending:[/]   {s.get('pending', 0)}\n"
            f"[blue]Retrying:[/]  {s.get('retrying', 0)}\n"
            f"[red]Exhausted:[/] {s.get('exhausted', 0)}\n"
            f"[green]Resolved:[/]  {s.get('resolved', 0)}",
            title="Error Queue",
            border_style="bright_blue",
        ))
        console.print()
