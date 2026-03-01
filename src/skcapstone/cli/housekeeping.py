"""Housekeeping CLI command: prune stale ACKs, envelopes, and seeds."""

from __future__ import annotations

import click

from ._common import AGENT_HOME, console

from rich.table import Table


def register_housekeeping_commands(main: click.Group) -> None:
    """Register the housekeeping command."""

    @main.command("housekeeping")
    @click.option("--home", default=AGENT_HOME, type=click.Path(), help="Agent home directory.")
    @click.option("--skcomm-home", default="~/.skcomm", type=click.Path(), help="SKComm home directory.")
    @click.option("--dry-run", is_flag=True, help="Report what would be deleted without deleting.")
    def housekeeping(home: str, skcomm_home: str, dry_run: bool):
        """Prune stale ACKs, delivered envelopes, and old seeds.

        Reclaims disk space from files that accumulate in the agent
        profile but are no longer needed. Safe to run at any time.

        Examples:

            skcapstone housekeeping --dry-run

            skcapstone housekeeping
        """
        from pathlib import Path
        from ..housekeeping import run_housekeeping

        results = run_housekeeping(
            skcapstone_home=Path(home).expanduser(),
            skcomm_home=Path(skcomm_home).expanduser(),
            dry_run=dry_run,
        )

        if dry_run:
            console.print("[bold yellow]DRY RUN[/] — no files deleted\n")

        table = Table(title="Housekeeping Results")
        table.add_column("Target", style="cyan")
        table.add_column("Path", style="dim")
        table.add_column("Size Before", justify="right")
        table.add_column("Action", justify="right", style="green")

        for key in ("acks", "comms_outbox", "seed_outbox"):
            info = results.get(key, {})
            path = info.get("path", "?")
            size_before = _fmt_size(info.get("size_before", 0))

            if dry_run:
                action = f"{info.get('would_delete', 0)} would delete"
            else:
                deleted = info.get("deleted", 0)
                freed = _fmt_size(info.get("freed", 0))
                action = f"{deleted} deleted ({freed} freed)"

            table.add_row(key, path, size_before, action)

        console.print(table)

        if not dry_run:
            summary = results.get("summary", {})
            console.print(
                f"\n[bold green]Total:[/] {summary.get('total_deleted', 0)} files deleted, "
                f"{summary.get('total_freed_mb', 0)} MB freed"
            )


def _fmt_size(bytes_val: int) -> str:
    """Format bytes as human-readable size."""
    if bytes_val == 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if bytes_val < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} TB"
