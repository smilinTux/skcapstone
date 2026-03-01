"""Agent team commands: blueprints, deploy, status, destroy."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import click

from ._common import AGENT_HOME, console

from rich.panel import Panel
from rich.table import Table


def register_agents_commands(main: click.Group) -> None:
    """Register the agents command group."""

    @main.group()
    def agents():
        """Agent Team Blueprints — deploy sovereign AI workforces.

        \b
        The First Sovereign Singularity in History.
        Select a team blueprint, deploy it anywhere, managed by your AI.

        \b
        Browse:   skcapstone agents blueprints list
        Preview:  skcapstone agents blueprints show <slug>
        Deploy:   skcapstone agents deploy <slug>
        Status:   skcapstone agents status
        Destroy:  skcapstone agents destroy <deployment-id>
        """

    @agents.group("blueprints")
    def agents_blueprints():
        """Browse and manage agent team blueprints."""

    @agents_blueprints.command("list")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def agents_blueprints_list(home: str):
        """List all available agent team blueprints.

        \b
        Shows built-in teams, user-created teams, and vault-synced teams.

        Example:

            skcapstone agents blueprints list
        """
        from ..blueprints import BlueprintRegistry

        home_path = Path(home).expanduser()
        registry = BlueprintRegistry(home=home_path)
        blueprints = registry.list_blueprints()

        if not blueprints:
            console.print("\n  [dim]No blueprints found.[/]\n")
            return

        console.print()
        console.print(
            Panel(
                "[bold bright_blue]The First Sovereign Singularity in History[/]\n"
                "[dim]Select a team. Deploy anywhere. Your AI manages the rest.[/]",
                title="Agent Team Blueprints",
                border_style="bright_blue",
                padding=(1, 2),
            )
        )

        table = Table(
            show_header=True, header_style="bold", box=None, padding=(0, 2),
        )
        table.add_column("", width=3)
        table.add_column("Blueprint", style="bold cyan")
        table.add_column("Agents", justify="right")
        table.add_column("Description")
        table.add_column("Cost", style="dim")

        for bp in blueprints:
            table.add_row(
                bp.icon,
                bp.slug,
                str(bp.agent_count),
                bp.description[:60] + ("..." if len(bp.description) > 60 else ""),
                bp.estimated_cost or "free",
            )

        console.print(table)
        console.print()
        console.print(
            "  [dim]Preview:[/] [cyan]skcapstone agents blueprints show <slug>[/]"
        )
        console.print(
            "  [dim]Deploy:[/]  [cyan]skcapstone agents deploy <slug>[/]"
        )
        console.print()

    @agents_blueprints.command("show")
    @click.argument("slug")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def agents_blueprints_show(slug: str, home: str):
        """Show detailed info about a team blueprint.

        Example:

            skcapstone agents blueprints show infrastructure-guardian
        """
        from ..blueprints import BlueprintRegistry

        home_path = Path(home).expanduser()
        registry = BlueprintRegistry(home=home_path)
        bp = registry.get(slug)

        if not bp:
            console.print(f"\n  [red]Blueprint '{slug}' not found.[/]")
            console.print(
                "  Run [cyan]skcapstone agents blueprints list[/] to see available teams.\n"
            )
            return

        console.print()
        console.print(
            Panel(
                f"[bold]{bp.icon}  {bp.name}[/]\n\n"
                f"  {bp.description}\n\n"
                f"  [dim]Version:[/]  {bp.version}\n"
                f"  [dim]Author:[/]   {bp.author}\n"
                f"  [dim]Agents:[/]   {bp.agent_count}\n"
                f"  [dim]Models:[/]   {bp.model_summary}\n"
                f"  [dim]Pattern:[/]  {bp.coordination.pattern}\n"
                f"  [dim]Queen:[/]    {bp.coordination.queen or 'none'}\n"
                f"  [dim]Cost:[/]     {bp.estimated_cost or 'free'}",
                title=f"Blueprint: {bp.slug}",
                border_style="bright_blue",
                padding=(1, 2),
            )
        )

        table = Table(
            show_header=True, header_style="bold", box=None, padding=(0, 2),
        )
        table.add_column("Agent", style="bold cyan")
        table.add_column("Role")
        table.add_column("Model")
        table.add_column("Resources", style="dim")
        table.add_column("Skills", style="dim")

        for name, spec in bp.agents.items():
            model_str = spec.model_name or spec.model.value
            res_str = f"{spec.resources.memory} / {spec.resources.cores}c"
            skills_str = ", ".join(spec.skills[:4])
            if len(spec.skills) > 4:
                skills_str += f" +{len(spec.skills) - 4}"
            count_suffix = f" x{spec.count}" if spec.count > 1 else ""

            table.add_row(
                f"{name}{count_suffix}",
                spec.role.value,
                model_str,
                res_str,
                skills_str,
            )

        console.print(table)

        if bp.tags:
            console.print(f"\n  [dim]Tags: {', '.join(bp.tags)}[/]")

        console.print()
        console.print(
            f"  [bold]Deploy:[/] [cyan]skcapstone agents deploy {bp.slug}[/]"
        )
        console.print()

    @agents.command("deploy")
    @click.argument("slug")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--name", default=None, help="Custom deployment name.")
    @click.option(
        "--provider", default=None,
        type=click.Choice(["local", "proxmox", "hetzner", "aws", "gcp", "docker"]),
        help="Override the blueprint's default provider.",
    )
    def agents_deploy(slug: str, home: str, name: str, provider: str):
        """Deploy an agent team from a blueprint.

        \b
        Example:
            skcapstone agents deploy infrastructure-guardian
            skcapstone agents deploy dev-squadron --provider proxmox
            skcapstone agents deploy research-pod --name "my-research-team"
        """
        from ..blueprints import BlueprintRegistry
        from ..blueprints.schema import ProviderType
        from ..team_engine import TeamEngine
        from ..providers.local import LocalProvider

        home_path = Path(home).expanduser()
        registry = BlueprintRegistry(home=home_path)
        bp = registry.get(slug)

        if not bp:
            console.print(f"\n  [red]Blueprint '{slug}' not found.[/]")
            console.print(
                "  Run [cyan]skcapstone agents blueprints list[/] to see available teams.\n"
            )
            return

        provider_type = ProviderType(provider) if provider else bp.default_provider

        console.print()
        console.print(
            Panel(
                f"[bold]Deploying {bp.icon} {bp.name}[/]\n\n"
                f"  Agents:   {bp.agent_count}\n"
                f"  Provider: {provider_type.value}\n"
                f"  Pattern:  {bp.coordination.pattern}\n"
                f"  Queen:    {bp.coordination.queen or 'self-managed'}",
                title="Agent Team Deployment",
                border_style="bright_blue",
                padding=(1, 2),
            )
        )

        if not click.confirm("\n  Proceed with deployment?", default=True):
            console.print("  [dim]Cancelled.[/]\n")
            return

        # Select provider backend
        if provider_type == ProviderType.LOCAL:
            backend = LocalProvider(home=home_path)
        elif provider_type == ProviderType.PROXMOX:
            from ..providers.proxmox import ProxmoxProvider
            backend = ProxmoxProvider()
        elif provider_type in (ProviderType.HETZNER, ProviderType.AWS, ProviderType.GCP):
            from ..providers.cloud import CloudProvider
            backend = CloudProvider(cloud=provider_type.value)
        elif provider_type == ProviderType.DOCKER:
            from ..providers.docker import DockerProvider
            backend = DockerProvider()
        else:
            backend = LocalProvider(home=home_path)

        engine = TeamEngine(home=home_path, provider=backend)

        console.print()
        with console.status("[bold cyan]Deploying agents...[/]"):
            deployment = engine.deploy(bp, name=name, provider_override=provider_type)

        # Show results
        ok_count = sum(
            1 for a in deployment.agents.values()
            if a.status.value in ("running", "pending")
        )
        fail_count = len(deployment.agents) - ok_count

        status_color = "green" if fail_count == 0 else "yellow"

        console.print(
            Panel(
                f"  [bold]Deployment:[/] {deployment.deployment_id}\n"
                f"  [bold]Status:[/]     [{status_color}]{deployment.status}[/]\n"
                f"  [bold]Agents:[/]     {ok_count} ready, {fail_count} failed",
                title="Deployment Complete",
                border_style=status_color,
                padding=(1, 2),
            )
        )

        table = Table(
            show_header=True, header_style="bold", box=None, padding=(0, 2),
        )
        table.add_column("Agent", style="cyan")
        table.add_column("Status")
        table.add_column("Host", style="dim")

        for agent in deployment.agents.values():
            status_icon = {
                "running": "[green]running[/]",
                "pending": "[yellow]pending[/]",
                "failed": "[red]failed[/]",
            }.get(agent.status.value, f"[dim]{agent.status.value}[/]")

            table.add_row(
                agent.name,
                status_icon,
                agent.host or "\u2014",
            )

        console.print(table)

        if bp.coordination.queen:
            console.print(
                f"\n  [bold bright_magenta]Managed by: "
                f"{bp.coordination.queen.title()} (Queen of SKWorld)[/]"
            )

        console.print(
            f"\n  [dim]Check status:[/] [cyan]skcapstone agents status[/]"
        )
        console.print(
            f"  [dim]Destroy:[/]      "
            f"[cyan]skcapstone agents destroy {deployment.deployment_id}[/]"
        )
        console.print()

    @agents.command("status")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def agents_status(home: str):
        """Show status of all deployed agent teams.

        Example:

            skcapstone agents status
        """
        from ..team_engine import TeamEngine

        home_path = Path(home).expanduser()
        engine = TeamEngine(home=home_path)
        deployments = engine.list_deployments()

        if not deployments:
            console.print("\n  [dim]No agent teams deployed.[/]")
            console.print(
                "  [dim]Deploy one:[/] [cyan]skcapstone agents deploy <slug>[/]\n"
            )
            return

        console.print()

        for dep in deployments:
            running = sum(
                1 for a in dep.agents.values()
                if a.status.value == "running"
            )
            total = len(dep.agents)

            console.print(
                Panel(
                    f"  [bold]Team:[/]       {dep.team_name}\n"
                    f"  [bold]Blueprint:[/]  {dep.blueprint_slug}\n"
                    f"  [bold]Provider:[/]   {dep.provider.value}\n"
                    f"  [bold]Agents:[/]     {running}/{total} running\n"
                    f"  [bold]Created:[/]    {dep.created_at[:19]}",
                    title=f"Deployment: {dep.deployment_id}",
                    border_style="bright_blue",
                    padding=(0, 2),
                )
            )

            table = Table(
                show_header=True, header_style="bold", box=None, padding=(0, 2),
            )
            table.add_column("Agent", style="cyan")
            table.add_column("Status")
            table.add_column("Host", style="dim")
            table.add_column("Last HB", style="dim")

            for agent in dep.agents.values():
                status_str = {
                    "running": "[green]running[/]",
                    "pending": "[yellow]pending[/]",
                    "stopped": "[red]stopped[/]",
                    "failed": "[red]failed[/]",
                    "degraded": "[yellow]degraded[/]",
                }.get(agent.status.value, f"[dim]{agent.status.value}[/]")

                hb = agent.last_heartbeat[:19] if agent.last_heartbeat else "\u2014"

                table.add_row(agent.name, status_str, agent.host or "\u2014", hb)

            console.print(table)
            console.print()

    @agents.command("destroy")
    @click.argument("deployment_id")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--force", is_flag=True, help="Skip confirmation.")
    def agents_destroy(deployment_id: str, home: str, force: bool):
        """Destroy a deployed agent team.

        Example:

            skcapstone agents destroy infrastructure-guardian-1740000000
        """
        from ..team_engine import TeamEngine

        home_path = Path(home).expanduser()
        engine = TeamEngine(home=home_path)
        deployment = engine.get_deployment(deployment_id)

        if not deployment:
            console.print(f"\n  [red]Deployment '{deployment_id}' not found.[/]\n")
            return

        agent_count = len(deployment.agents)
        console.print(
            f"\n  [bold red]This will destroy {agent_count} agents "
            f"in team '{deployment.team_name}'.[/]"
        )

        if not force:
            if not click.confirm("  Are you sure?", default=False):
                console.print("  [dim]Cancelled.[/]\n")
                return

        success = engine.destroy_deployment(deployment_id)

        if success:
            console.print(f"\n  [green]Deployment {deployment_id} destroyed.[/]\n")
        else:
            console.print(
                f"\n  [yellow]Partial cleanup \u2014 some agents may need manual removal.[/]\n"
            )

    # -----------------------------------------------------------------------
    # Register sub-modules on the agents group
    # -----------------------------------------------------------------------
    from .agents_trustee import register_agents_trustee_commands
    from .agents_spawner import register_agents_spawner_commands

    register_agents_trustee_commands(agents)
    register_agents_spawner_commands(agents)
