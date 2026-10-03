"""Backup and restore commands: create, restore, list."""

from __future__ import annotations

from pathlib import Path

import click
from rich.panel import Panel
from rich.table import Table

from ._common import AGENT_HOME, console
from ._validators import validate_file_path


def register_backup_commands(main: click.Group) -> None:
    """Register the backup command group."""

    @main.group()
    def backup():
        """Backup and restore - portable sovereign agent state.

        Create encrypted backups of your full agent state and
        restore on any machine. Your identity travels with you.
        """

    @backup.command("create")
    @click.option(
        "--home",
        default=None,
        type=click.Path(),
        help="Agent home directory. Overrides --agent when set.",
    )
    @click.option("--agent", default=None, help="Agent name (e.g. lumina, opus).")
    @click.option("--output", "-o", default=None, type=click.Path(), help="Output directory.")
    def backup_create(home: str, agent: str, output: str):
        """Create a full backup of the sovereign agent state.

        Archives identity, memories, trust, config, coordination,
        and agent card into a compressed tarball with integrity checksums.

        By default this backs up the active agent's per-agent home
        (~/.skcapstone/agents/<name>/), NOT the shared operator root -
        that is where the flat memory tiers actually live.

        Examples:

            skcapstone backup create

            skcapstone backup create --agent opus

            skcapstone backup create -o /mnt/usb/backups
        """
        from .. import SKCAPSTONE_AGENT, agent_home
        from ..backup import create_backup

        # --home wins if given; otherwise resolve the per-agent home so the
        # flat memory tiers (agents/<name>/memory/{short,mid,long}-term) are
        # captured instead of the near-empty shared root memory/ dir.
        agent_name = agent or SKCAPSTONE_AGENT
        if home:
            home_path = Path(home).expanduser()
        else:
            home_path = agent_home(agent_name or None)
        out_dir = Path(output).expanduser() if output else None

        try:
            console.print(f"\n[cyan]Creating backup[/] [dim]({home_path})[/]...")
            result = create_backup(
                home=home_path,
                output_dir=out_dir,
                agent_name=agent_name or "",
            )

            size_mb = result["archive_size"] / 1024 / 1024
            console.print(
                Panel(
                    f"[bold green]Backup created[/]\n"
                    f"ID: {result['backup_id']}\n"
                    f"Files: {result['file_count']}\n"
                    f"Size: {size_mb:.1f} MB\n"
                    f"Path: [cyan]{result['filepath']}[/]",
                    title="Backup Complete",
                    border_style="green",
                )
            )
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

        validate_file_path(archive)

        target = Path(home).expanduser()

        try:
            console.print(f"\n[cyan]Restoring from {archive}...[/]")
            result = restore_backup(
                archive_path=archive,
                target_home=target,
                verify=not no_verify,
            )

            status = "[green]VERIFIED[/]" if result["verified"] else "[red]ERRORS[/]"
            console.print(
                Panel(
                    f"[bold green]Restore complete[/]\n"
                    f"Agent: {result['agent_name']}\n"
                    f"Files: {result['file_count']}\n"
                    f"Target: [cyan]{result['target']}[/]\n"
                    f"Integrity: {status}",
                    title="Restore Complete",
                    border_style="green",
                )
            )

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

    @backup.command("gfs")
    @click.option(
        "--home",
        default=None,
        type=click.Path(),
        help="Agent home directory. Overrides --agent when set.",
    )
    @click.option("--agent", default=None, help="Agent name (e.g. lumina, opus).")
    @click.option(
        "--output",
        "-o",
        default=None,
        type=click.Path(),
        help="Backup directory (default: config or <home>/backups).",
    )
    @click.option("--json", "as_json", is_flag=True, help="Emit the result as JSON.")
    def backup_gfs(home: str, agent: str, output: str, as_json: bool):
        """Run the GFS backup job: create a backup then prune old ones.

        Creates a timestamped backup and applies Grandfather-Father-Son
        retention (keep N daily, M weekly, K monthly) within the backup
        directory. Destinations and retention counts are config-driven
        (config.yaml ``backup:`` block or ``SKCAPSTONE_BACKUP_*`` env vars).

        This is the command wired to the systemd timer / scheduler job.

        Examples:

            skcapstone backup gfs

            skcapstone backup gfs --agent opus -o /mnt/usb/backups
        """
        import json as _json

        from .. import SKCAPSTONE_AGENT, agent_home
        from ..gfs_backup import resolve_config, run_backup_job

        agent_name = agent or SKCAPSTONE_AGENT
        if home:
            home_path = Path(home).expanduser()
        else:
            home_path = agent_home(agent_name or None)

        overrides = {"dir": output} if output else None
        cfg = resolve_config(home_path, agent_name=agent_name or "", overrides=overrides)

        try:
            result = run_backup_job(home=home_path, config=cfg)
        except Exception as exc:  # noqa: BLE001 - surface a clean CLI error
            console.print(f"[red]GFS backup failed: {exc}[/]")
            raise SystemExit(1)

        if as_json:
            console.print(_json.dumps(result, indent=2))
            return

        action = (
            "Created"
            if result["created"]
            else ("Skipped (fresh)" if result["skipped"] else "No-op")
        )
        console.print(
            Panel(
                f"[bold green]GFS backup complete[/]\n"
                f"Action: {action}\n"
                f"Backup ID: {result.get('backup_id') or '-'}\n"
                f"Kept: {result['kept']}\n"
                f"Pruned: {result['pruned']}\n"
                f"Dir: [cyan]{result['backup_dir']}[/]",
                title="GFS Backup",
                border_style="green",
            )
        )

    @backup.command("health")
    @click.option(
        "--home",
        default=None,
        type=click.Path(),
        help="Agent home directory. Overrides --agent when set.",
    )
    @click.option("--agent", default=None, help="Agent name (e.g. lumina, opus).")
    @click.option(
        "--output",
        "-o",
        default=None,
        type=click.Path(),
        help="Backup directory to check (default: config or <home>/backups).",
    )
    @click.option("--json", "as_json", is_flag=True, help="Emit the status object as JSON.")
    def backup_health(home: str, agent: str, output: str, as_json: bool):
        """Report backup freshness (ok / stale / missing / failed).

        Checks the age of the most recent backup against the freshness
        threshold and the last recorded run outcome. Exits non-zero when
        unhealthy so it is usable as a monitoring probe.

        Examples:

            skcapstone backup health

            skcapstone backup health --json
        """
        import json as _json

        from .. import SKCAPSTONE_AGENT, agent_home
        from ..gfs_backup import check_backup_health, resolve_config

        agent_name = agent or SKCAPSTONE_AGENT
        if home:
            home_path = Path(home).expanduser()
        else:
            home_path = agent_home(agent_name or None)

        overrides = {"dir": output} if output else None
        cfg = resolve_config(home_path, agent_name=agent_name or "", overrides=overrides)
        report = check_backup_health(home=home_path, config=cfg)

        if as_json:
            console.print(_json.dumps(report, indent=2))
        else:
            color = "green" if report["healthy"] else "red"
            console.print(
                Panel(
                    f"[bold {color}]{report['status'].upper()}[/]\n"
                    f"{report['message']}\n"
                    f"Backups: {report['backup_count']}\n"
                    f"Dir: [cyan]{report['backup_dir']}[/]",
                    title="Backup Health",
                    border_style=color,
                )
            )

        if not report["healthy"]:
            raise SystemExit(1)

    main.add_command(backup)
