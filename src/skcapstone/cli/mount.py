"""FUSE mount commands: start, stop, status."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from ._common import AGENT_HOME, console

from rich.panel import Panel
from rich.table import Table


def register_mount_commands(main: click.Group) -> None:
    """Register the mount command group."""

    @main.group()
    def mount():
        """Sovereign FUSE filesystem — browse agent data as files.

        \b
        Mount the sovereign virtual filesystem to access memories, identity,
        inbox, outbox, and coordination tasks as ordinary files.

        \b
        Mount:    skcapstone mount start
        Debug:    skcapstone mount start --foreground
        Unmount:  skcapstone mount stop
        Status:   skcapstone mount status
        """

    @mount.command("start")
    @click.option(
        "--mount-point",
        default="~/.sovereign/mount",
        type=click.Path(),
        help="Directory to mount the sovereign filesystem at.",
        show_default=True,
    )
    @click.option(
        "--home",
        default=AGENT_HOME,
        type=click.Path(),
        help="Agent home directory.",
    )
    @click.option(
        "--foreground",
        "foreground",
        is_flag=True,
        default=False,
        help="Run in foreground (blocks; useful for debugging).",
    )
    def mount_start(mount_point: str, home: str, foreground: bool):
        """Mount the sovereign virtual filesystem.

        Exposes memories, identity, inbox, outbox, and coordination tasks
        as a read-mostly POSIX filesystem via FUSE.

        \b
        Requires: pip install skcapstone[fuse]

        \b
        Examples:

            skcapstone mount start

            skcapstone mount start --mount-point /mnt/sovereign

            skcapstone mount start --foreground
        """
        from ..fuse_mount import FUSEDaemon

        mount_path = Path(mount_point).expanduser()
        home_path = Path(home).expanduser()

        daemon = FUSEDaemon(mount_point=mount_path, agent_home=home_path)

        if foreground:
            console.print(
                f"[bold cyan]Mounting sovereign filesystem at [white]{mount_path}[/] "
                f"[dim](foreground — Ctrl-C to unmount)[/]"
            )
        else:
            console.print(
                f"[bold cyan]Mounting sovereign filesystem at [white]{mount_path}[/] ..."
            )

        ok = daemon.start(foreground=foreground)

        if ok and not foreground:
            console.print(f"[green]Mounted.[/] [dim]Unmount with: skcapstone mount stop[/]")
        elif not ok:
            console.print("[bold red]Mount failed.[/] Check logs or try --foreground for details.")
            sys.exit(1)

    @mount.command("stop")
    @click.option(
        "--mount-point",
        default="~/.sovereign/mount",
        type=click.Path(),
        help="Mount point to unmount.",
        show_default=True,
    )
    @click.option(
        "--home",
        default=AGENT_HOME,
        type=click.Path(),
        help="Agent home directory.",
    )
    def mount_stop(mount_point: str, home: str):
        """Unmount the sovereign virtual filesystem.

        \b
        Example:

            skcapstone mount stop
        """
        from ..fuse_mount import FUSEDaemon

        mount_path = Path(mount_point).expanduser()
        home_path = Path(home).expanduser()

        daemon = FUSEDaemon(mount_point=mount_path, agent_home=home_path)
        console.print(f"[bold cyan]Unmounting {mount_path} ...[/]")

        ok = daemon.stop()
        if ok:
            console.print("[green]Unmounted.[/]")
        else:
            console.print(
                "[bold red]Unmount failed.[/] "
                f"[dim]Try manually: fusermount -u {mount_path}[/]"
            )
            sys.exit(1)

    @mount.command("status")
    @click.option(
        "--mount-point",
        default="~/.sovereign/mount",
        type=click.Path(),
        help="Mount point to check.",
        show_default=True,
    )
    @click.option(
        "--home",
        default=AGENT_HOME,
        type=click.Path(),
        help="Agent home directory.",
    )
    @click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
    def mount_status(mount_point: str, home: str, as_json: bool):
        """Show the status of the sovereign FUSE filesystem.

        \b
        Example:

            skcapstone mount status

            skcapstone mount status --json
        """
        from ..fuse_mount import FUSEDaemon

        mount_path = Path(mount_point).expanduser()
        home_path = Path(home).expanduser()

        daemon = FUSEDaemon(mount_point=mount_path, agent_home=home_path)
        status = daemon.status()

        if as_json:
            click.echo(json.dumps(status, indent=2))
            return

        mounted = status.get("mounted", False)
        icon = "[bold green]MOUNTED[/]" if mounted else "[bold red]NOT MOUNTED[/]"
        pid = status.get("pid")
        updated = status.get("updated_at", "—")

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Key", style="dim")
        table.add_column("Value")

        table.add_row("Status", icon)
        table.add_row("Mount point", str(status.get("mount_point", "")))
        table.add_row("Agent home", str(status.get("agent_home", "")))
        table.add_row("PID", str(pid) if pid else "[dim]—[/]")
        table.add_row("Last updated", updated or "[dim]—[/]")

        console.print()
        console.print(Panel(table, title="[bold]Sovereign Filesystem Status[/]", border_style="cyan"))
        console.print()
