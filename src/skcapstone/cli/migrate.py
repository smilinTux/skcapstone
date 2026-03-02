"""CLI commands for multi-agent migration."""

from __future__ import annotations

import click

from ._common import console


def register_migrate_commands(cli: click.Group) -> None:
    """Register the 'migrate' command group."""
    cli.add_command(migrate_cmd)


@click.command("migrate")
@click.option("--agent", required=True, help="Agent name (e.g., opus)")
@click.option("--dry-run", is_flag=True, help="Show what would happen without moving")
@click.option("--root", default="~/.skcapstone", help="skcapstone root directory")
def migrate_cmd(agent: str, dry_run: bool, root: str) -> None:
    """Migrate to multi-agent household layout.

    Moves per-agent data into agents/{name}/ and creates symlinks
    at old paths for backward compatibility.
    """
    from pathlib import Path

    from ..migrate_multi_agent import migrate_to_multi_agent

    results = migrate_to_multi_agent(
        root=Path(root),
        agent_name=agent,
        dry_run=dry_run,
    )

    if dry_run:
        console.print("[yellow]DRY RUN[/] — no files will be moved\n")

    if results["moved"]:
        console.print("[green]Moved:[/]")
        for item in results["moved"]:
            console.print(f"  {item}")

    if results["symlinks_created"]:
        console.print("\n[cyan]Symlinks created:[/]")
        for link in results["symlinks_created"]:
            console.print(f"  {link}")

    if results["skipped"]:
        console.print("\n[dim]Skipped:[/]")
        for item in results["skipped"]:
            console.print(f"  {item}")

    if results["errors"]:
        console.print("\n[red]Errors:[/]")
        for err in results["errors"]:
            console.print(f"  {err}")

    if not results["moved"] and not dry_run:
        console.print("[dim]Nothing to migrate.[/]")
    elif not dry_run:
        console.print(f"\n[green]Migration complete.[/] Agent home: {results['agent_home']}")
        console.print(f"[dim]Use: SKCAPSTONE_AGENT={agent} skcapstone status[/]")
