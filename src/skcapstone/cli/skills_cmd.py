"""Skills commands: list, install."""

from __future__ import annotations

import json
import sys

import click

from ._common import AGENT_HOME, console
from ..registry_client import get_registry_client

from rich.panel import Panel
from rich.table import Table


def register_skills_commands(main: click.Group) -> None:
    """Register the skills command group."""

    @main.group()
    def skills():
        """Remote skills registry — discover and install agent skills.

        Browse skills at skills.smilintux.org, search by name or tag,
        and install skill packages into your local agent namespace.

        Set SKSKILLS_REGISTRY_URL to override the default registry.
        """

    @skills.command("list")
    @click.option("--query", "-q", default="", help="Filter by name, description, or tag.")
    @click.option(
        "--registry",
        default=None,
        envvar="SKSKILLS_REGISTRY_URL",
        help="Override the skills registry URL.",
    )
    @click.option("--json", "json_out", is_flag=True, help="Output raw JSON.")
    def skills_list(query: str, registry: str | None, json_out: bool) -> None:
        """List skills available in the remote registry.

        Without --query all skills are shown. With --query only skills
        matching the name, description, or tags are returned.

        Examples:

            skcapstone skills list

            skcapstone skills list --query syncthing

            skcapstone skills list --query identity --json
        """
        client = get_registry_client(registry)
        if client is None:
            console.print(
                "[bold red]skskills not installed.[/] "
                "Run: pip install skskills"
            )
            sys.exit(1)

        try:
            skill_entries = client.search(query) if query else client.list_skills()
        except Exception as exc:
            console.print(f"[bold red]Registry error:[/] {exc}")
            sys.exit(1)

        if json_out:
            click.echo(json.dumps(skill_entries, indent=2))
            return

        if not skill_entries:
            suffix = f" matching '{query}'" if query else ""
            console.print(f"\n  [dim]No skills found{suffix}.[/]\n")
            return

        label = f"[bold]{len(skill_entries)}[/] skill(s)"
        if query:
            label += f" matching [cyan]'{query}'[/]"

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Name", style="cyan")
        table.add_column("Version", style="dim")
        table.add_column("Description")
        table.add_column("Tags", style="dim")

        for s in skill_entries:
            table.add_row(
                s.get("name", ""),
                s.get("version", ""),
                s.get("description", ""),
                ", ".join(s.get("tags", [])),
            )

        console.print()
        console.print(Panel(label, title="Skills Registry", border_style="bright_blue"))
        console.print(table)
        console.print()

    @skills.command("install")
    @click.argument("name")
    @click.option("--version", default=None, help="Specific version (default: latest).")
    @click.option(
        "--agent",
        default="global",
        help="Agent namespace for installation (default: global).",
    )
    @click.option("--force", is_flag=True, help="Overwrite an existing installation.")
    @click.option(
        "--registry",
        default=None,
        envvar="SKSKILLS_REGISTRY_URL",
        help="Override the skills registry URL.",
    )
    def skills_install(
        name: str,
        version: str | None,
        agent: str,
        force: bool,
        registry: str | None,
    ) -> None:
        """Download and install a skill from the remote registry.

        Fetches the skill package, verifies its checksum, and installs it
        into the local SKSkills directory for the specified agent namespace.

        Examples:

            skcapstone skills install syncthing-setup

            skcapstone skills install pgp-identity --version 0.2.0

            skcapstone skills install syncthing-setup --agent opus
        """
        client = get_registry_client(registry)
        if client is None:
            console.print(
                "[bold red]skskills not installed.[/] "
                "Run: pip install skskills"
            )
            sys.exit(1)

        ver_label = f" @{version}" if version else ""
        agent_label = f" (agent: {agent})" if agent != "global" else ""
        console.print(
            f"\n  Installing [cyan]{name}[/][dim]{ver_label}{agent_label}[/] ...\n"
        )

        try:
            result = client.install(name, version=version, agent=agent, force=force)
        except FileNotFoundError:
            console.print(
                f"[bold red]Not found:[/] skill [cyan]{name}[/] is not in the registry.\n"
                f"  Run [dim]skcapstone skills list --query {name}[/] to search."
            )
            console.print()
            sys.exit(1)
        except ValueError as exc:
            console.print(f"[bold red]Install failed:[/] {exc}\n")
            sys.exit(1)
        except Exception as exc:
            console.print(f"[bold red]Error:[/] {exc}\n")
            sys.exit(1)

        console.print(
            f"  [green]Installed:[/] [bold]{result['name']}[/] v{result['version']}"
        )
        console.print(f"  [dim]Path:  {result['install_path']}[/]")
        console.print(f"  [dim]Agent: {result['agent']}[/]\n")
