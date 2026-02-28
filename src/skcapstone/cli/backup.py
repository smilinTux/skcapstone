"""Backup and restore commands: create, restore, list."""

from __future__ import annotations

from pathlib import Path

import click

from ._common import AGENT_HOME, console

from rich.panel import Panel
from rich.table import Table


def register_backup_commands(main: click.Group) -> None:
    """Register the backup command group."""

    @main.group()
    def backup():
        """Backup and restore — portable sovereign agent state.

        Create encrypted backups of your full agent state and
        restore on any machine. Your identity travels with you.
        """

    @backup.command("create")
    @click.option("--home", default=AGENT_HOME, type=click.Path(), help="Agent home directory.")
    @click.option("--output", "-o", default=None, type=click.Path(), help="Output directory.")
    def backup_create(home: str, output: str):
        """Create a full backup of the sovereign agent state.

        Archives identity, memories, trust, config, coordination,
        and agent card into a compressed tarball with integrity checksums.

        Examples:

            skcapstone backup create

            skcapstone backup create -o /mnt/usb/backups
        """
        from ..backup import create_backup

        home_path = Path(home).expanduser()
        out_dir = Path(output).expanduser() if output else None

        try:
            console.print("\n[cyan]Creating backup...[/]")
            result = create_backup(home=home_path, output_dir=out_dir)

            size_mb = result["archive_size"] / 1024 / 1024
            console.print(Panel(
                f"[bold green]Backup created[/]\n"
                f"ID: {result['backup_id']}\n"
                f"Files: {result['file_count']}\n"
                f"Size: {size_mb:.1f} MB\n"
                f"Path: [cyan]{result['filepath']}[/]",
                title="Backup Complete",
                border_style="green",
            ))
        except FileNotFoundError as exc:
            console.print(f"[red]{exc}[/]")
            raise SystemExit(1)

    @backup.command("restore")
    @click.argument("archive")
    @click.option("--home", default=AGENT_HOME, type=click.Path(), help="Target home directory.")
    @click.option("--no-verify", is_flag=True, help="Skip checksum verification.")
    def backup_restore(archive: str, home: str, no_verify: bool):
        """Restore the agent from a backup archive.

        Extracts the backup and verifies file integrity.

        Examples:

            skcapstone backup restore backup-20260224.tar.gz

            skcapstone backup restore /mnt/usb/backup.tar.gz --home ~/.skcapstone-new
        """
        from ..backup import restore_backup

        target = Path(home).expanduser()

        try:
            console.print(f"\n[cyan]Restoring from {archive}...[/]")
            result = restore_backup(
                archive_path=archive,
                target_home=target,
                verify=not no_verify,
            )

            status = "[green]VERIFIED[/]" if result["verified"] else "[red]ERRORS[/]"
            console.print(Panel(
                f"[bold green]Restore complete[/]\n"
                f"Agent: {result['agent_name']}\n"
                f"Files: {result['file_count']}\n"
                f"Target: [cyan]{result['target']}[/]\n"
                f"Integrity: {status}",
                title="Restore Complete",
                border_style="green",
            ))

            if result["errors"]:
                console.print("[yellow]Verification errors:[/]")
                for err in result["errors"]:
                    console.print(f"  [red]{err}[/]")
        except (FileNotFoundError, ValueError) as exc:
            console.print(f"[red]{exc}[/]")
            raise SystemExit(1)

    @backup.command("list")
    @click.option("--home", default=AGENT_HOME, type=click.Path(), help="Agent home directory.")
    def backup_list(home: str):
        """List available backups.

        Examples:

            skcapstone backup list
        """
        from ..backup import list_backups

        home_path = Path(home).expanduser()
        backups = list_backups(home_path / "backups")

        if not backups:
            console.print("\n[dim]No backups found.[/]\n")
            return

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Filename", style="cyan")
        table.add_column("Size", justify="right")
        table.add_column("Created", style="dim")

        for b in backups:
            size_mb = b["size"] / 1024 / 1024
            table.add_row(b["filename"], f"{size_mb:.1f} MB", b["created"][:19])

        console.print(f"\n[bold]{len(backups)}[/] backup(s):\n")
        console.print(table)
        console.print()

    main.add_command(backup)
