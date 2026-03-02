"""Archive command — manage conversation archival."""

from __future__ import annotations

from pathlib import Path

import click

from ._common import AGENT_HOME, console
from rich.panel import Panel
from rich.table import Table


def register_archive_commands(main: click.Group) -> None:
    """Register the archive command group."""

    @main.group()
    def archive():
        """Conversation archival — compress old messages to save space.

        Archives peer conversation messages older than 30 days that are
        not in the most-recent 100, compressing them into gzip files
        under ~/.skcapstone/archive/.
        """

    @archive.command("run")
    @click.option(
        "--home", default=AGENT_HOME, type=click.Path(), help="Agent home directory."
    )
    @click.option(
        "--age-days", default=30, show_default=True,
        help="Archive messages older than this many days.",
    )
    @click.option(
        "--keep-recent", default=100, show_default=True,
        help="Always keep this many recent messages per peer in the active file.",
    )
    @click.option(
        "--peer", default=None, help="Archive only this peer (omit to archive all)."
    )
    @click.option(
        "--dry-run", is_flag=True, help="Show what would be archived without making changes."
    )
    def archive_run(home: str, age_days: int, keep_recent: int, peer: str | None, dry_run: bool):
        """Run conversation archival.

        Scans active conversation files and moves messages older than
        AGE_DAYS days (that are outside the KEEP_RECENT window) into
        compressed .json.gz files under ~/.skcapstone/archive/.

        Examples:

            skcapstone archive run

            skcapstone archive run --age-days 7 --keep-recent 50

            skcapstone archive run --peer jarvis

            skcapstone archive run --dry-run
        """
        from ..archiver import ConversationArchiver

        home_path = Path(home).expanduser()
        archiver = ConversationArchiver(
            home_path, age_days=age_days, keep_recent=keep_recent
        )

        if dry_run:
            _dry_run_report(archiver, peer)
            return

        if peer:
            result = archiver.archive_peer(peer)
            if result.skipped:
                console.print(f"\n[dim]Nothing to archive for peer [bold]{peer}[/].[/]\n")
            else:
                console.print(Panel(
                    f"[bold green]Archived {result.archived_count} message(s)[/]\n"
                    f"Retained: {result.retained_count} message(s)\n"
                    f"Archive: [cyan]{result.archive_path}[/]",
                    title=f"Archive — {peer}",
                    border_style="green",
                ))
            return

        summary = archiver.archive_all()

        if summary.total_archived == 0:
            console.print("\n[dim]Nothing to archive — all conversations are current.[/]\n")
            return

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Peer", style="cyan")
        table.add_column("Archived", justify="right")
        table.add_column("Retained", justify="right")
        table.add_column("Archive file", style="dim")

        for r in summary.results:
            if r.skipped:
                continue
            table.add_row(
                r.peer,
                str(r.archived_count),
                str(r.retained_count),
                str(r.archive_path) if r.archive_path else "—",
            )

        console.print(f"\n[bold]Archived {summary.total_archived} message(s) across "
                      f"{summary.peers_processed - summary.peers_skipped} peer(s):[/]\n")
        console.print(table)
        console.print()

    @archive.command("list")
    @click.option(
        "--home", default=AGENT_HOME, type=click.Path(), help="Agent home directory."
    )
    def archive_list(home: str):
        """List all conversation archive files.

        Shows each peer's archive size and message count.

        Examples:

            skcapstone archive list
        """
        from ..archiver import ConversationArchiver

        home_path = Path(home).expanduser()
        archiver = ConversationArchiver(home_path)
        archives = archiver.list_archives()

        if not archives:
            console.print("\n[dim]No archives found.[/]\n")
            return

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Peer", style="cyan")
        table.add_column("Messages", justify="right")
        table.add_column("Size", justify="right")
        table.add_column("Path", style="dim")

        total_msgs = 0
        total_bytes = 0
        for a in archives:
            size_kb = a["size_bytes"] / 1024
            table.add_row(
                a["peer"],
                str(a["message_count"]),
                f"{size_kb:.1f} KB",
                str(a["path"]),
            )
            total_msgs += a["message_count"]
            total_bytes += a["size_bytes"]

        console.print(f"\n[bold]{len(archives)}[/] archive(s) — "
                      f"{total_msgs} messages, {total_bytes / 1024:.1f} KB total:\n")
        console.print(table)
        console.print()

    main.add_command(archive)


# ---------------------------------------------------------------------------
# Dry-run helper
# ---------------------------------------------------------------------------


def _dry_run_report(archiver, peer: str | None) -> None:
    """Print what would be archived without making changes."""
    from ..archiver import _load_messages

    conversations_dir = archiver._conversations_dir

    if not conversations_dir.exists():
        console.print("\n[dim]No conversations directory found.[/]\n")
        return

    files = (
        [conversations_dir / f"{peer}.json"]
        if peer
        else sorted(conversations_dir.glob("*.json"))
    )

    rows = []
    for conv_file in files:
        if not conv_file.exists():
            continue
        p = conv_file.stem
        messages = _load_messages(conv_file)
        retain, to_archive = archiver._partition(messages)
        rows.append((p, len(to_archive), len(retain)))

    if not rows or all(a == 0 for _, a, _ in rows):
        console.print("\n[dim][DRY RUN] Nothing would be archived.[/]\n")
        return

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("Peer", style="cyan")
    table.add_column("Would archive", justify="right", style="yellow")
    table.add_column("Would retain", justify="right", style="green")

    for p, a, r in rows:
        if a > 0:
            table.add_row(p, str(a), str(r))

    console.print("\n[bold yellow][DRY RUN][/] The following would be archived:\n")
    console.print(table)
    console.print()
