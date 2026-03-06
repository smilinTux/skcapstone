"""Sub-agent spawner commands: spawn, spawned, kill, babies."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import click

from ._common import AGENT_HOME, console

from rich.panel import Panel
from rich.table import Table


def _resolve_provider_backend(provider: Optional[str], home_path: Path):
    """Resolve provider string to backend object and ProviderType.

    Args:
        provider: Provider name string or None.
        home_path: Agent home path.

    Returns:
        Tuple of (provider_backend, provider_type).
    """
    from ..blueprints.schema import ProviderType

    prov_backend = None
    prov_type = None
    if provider:
        prov_type = ProviderType(provider)
        try:
            from ..providers import LocalProvider, DockerProvider, ProxmoxProvider
            if prov_type == ProviderType.LOCAL:
                prov_backend = LocalProvider(agents_root=home_path / "agents" / "local")
            elif prov_type == ProviderType.DOCKER:
                prov_backend = DockerProvider()
            elif prov_type == ProviderType.PROXMOX:
                prov_backend = ProxmoxProvider()
        except Exception:
            pass
    return prov_backend, prov_type


def _show_spawn_result(result) -> None:
    """Display spawn result to console.

    Args:
        result: SpawnResult from the spawner.
    """
    if result.status == "failed":
        console.print(
            f"\n  [red bold]Spawn failed:[/] {result.error}\n"
        )
    else:
        console.print(
            Panel(
                f"  [bold green]Agent spawned successfully![/]\n\n"
                f"  [bold]Name:[/]       [cyan]{result.agent_name}[/]\n"
                f"  [bold]Deployment:[/] [dim]{result.deployment_id}[/]\n"
                f"  [bold]Status:[/]     [green]{result.status.value if hasattr(result.status, 'value') else result.status}[/]\n"
                f"  [bold]Host:[/]       {result.host}\n"
                + (f"  [bold]PID:[/]        {result.pid}\n" if result.pid else "")
                + (f"  [bold]Claimed:[/]    {result.coord_task_id}\n" if result.coord_task_id else "")
                + f"\n  [dim]Kill:[/] [cyan]skcapstone agents kill {result.deployment_id}[/]",
                title="[bold green]Sub-Agent Spawned[/]",
                border_style="green",
            )
        )


def register_agents_spawner_commands(agents: click.Group) -> None:
    """Register sub-agent spawner commands on the agents group."""

    @agents.command("spawn")
    @click.argument("task")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option(
        "--provider", "-p", type=click.Choice(["local", "docker", "proxmox", "hetzner"]),
        default=None, help="Target provider (auto-selects if not specified).",
    )
    @click.option("--role", "-r", type=click.Choice([
        "manager", "worker", "researcher", "coder", "reviewer",
        "documentarian", "security", "ops",
    ]), default=None, help="Override auto-detected agent role.")
    @click.option("--model", "-m", type=click.Choice([
        "fast", "code", "reason", "nuance", "local",
    ]), default=None, help="Override auto-detected model tier.")
    @click.option("--skill", "-s", multiple=True, help="Skills to load (repeatable).")
    @click.option("--claim", "coord_task_id", default=None, help="Claim a coordination task.")
    @click.option("--name", "agent_name", default=None, help="Custom agent name.")
    @click.option("--baby", "-b", is_flag=True, default=False,
                  help="Treat TASK as a baby agent name instead of a free-text task.")
    def agents_spawn(
        task: str,
        home: str,
        provider: Optional[str],
        role: Optional[str],
        model: Optional[str],
        skill: tuple,
        coord_task_id: Optional[str],
        agent_name: Optional[str],
        baby: bool,
    ):
        """Spawn a task-specific sub-agent.

        Auto-detects the best role and model tier from the task description.
        Use --role and --model to override.

        If --baby is set, TASK is treated as a baby agent name (e.g.,
        'memory-curator'). The agent's pre-configured role, model, skills,
        and task are used automatically.

        \b
        Examples:
            skcapstone agents spawn "Write unit tests for capauth login"
            skcapstone agents spawn "Review the skchat architecture" --model reason
            skcapstone agents spawn --baby memory-curator
            skcapstone agents spawn --baby health-monitor --provider docker
            skcapstone agents spawn "Deploy monitoring" --provider docker --role ops
            skcapstone agents spawn "Research FUSE mounting" --claim dbfb78e3
        """
        from ..blueprints.schema import AgentRole, ModelTier, ProviderType
        from ..spawner import SubAgentSpawner, classify_task

        home_path = Path(home).expanduser()
        prov_backend, prov_type = _resolve_provider_backend(provider, home_path)

        # Baby agent mode: spawn a pre-defined baby agent by name
        if baby:
            from ..baby_agents import get_baby_agent, BABY_AGENTS

            baby_def = get_baby_agent(task)
            if baby_def is None:
                available = ", ".join(sorted(BABY_AGENTS.keys()))
                console.print(
                    f"\n  [red]Unknown baby agent '{task}'.[/]\n"
                    f"  Available: [cyan]{available}[/]\n"
                    f"  List all: [cyan]skcapstone agents babies[/]\n"
                )
                return

            console.print(
                Panel(
                    f"  [bold]Baby Agent:[/] [cyan]{baby_def.name}[/]\n"
                    f"  [bold]Role:[/]       [cyan]{baby_def.role.value}[/]\n"
                    f"  [bold]Model:[/]      [cyan]{baby_def.model.value}[/]\n"
                    f"  [bold]Skills:[/]     [dim]{', '.join(baby_def.skills)}[/]\n"
                    f"  [bold]Provider:[/]   [cyan]{provider or 'local'}[/]\n"
                    f"  [bold]Task:[/]       {baby_def.task}",
                    title="[bold]Spawning Baby Agent[/]",
                    border_style="bright_magenta",
                )
            )

            spawner = SubAgentSpawner(home=home_path, provider=prov_backend)

            with console.status("[bold magenta]Spawning baby agent...[/]"):
                result = spawner.spawn_baby(
                    name=baby_def.name,
                    provider=prov_type,
                )

            _show_spawn_result(result)
            return

        # Standard mode: spawn from free-text task description
        detected_role, detected_model = classify_task(task)
        final_role = AgentRole(role) if role else detected_role
        final_model = ModelTier(model) if model else detected_model

        console.print(
            Panel(
                f"  [bold]Task:[/]     {task}\n"
                f"  [bold]Role:[/]     [cyan]{final_role.value}[/]"
                f"{'  [dim](auto-detected)[/]' if not role else ''}\n"
                f"  [bold]Model:[/]    [cyan]{final_model.value}[/]"
                f"{'  [dim](auto-detected)[/]' if not model else ''}\n"
                f"  [bold]Provider:[/] [cyan]{provider or 'local'}[/]\n"
                + (f"  [bold]Claim:[/]    [cyan]{coord_task_id}[/]\n" if coord_task_id else ""),
                title="[bold]Spawning Sub-Agent[/]",
                border_style="bright_cyan",
            )
        )

        spawner = SubAgentSpawner(
            home=home_path,
            provider=prov_backend,
        )

        with console.status("[bold cyan]Spawning agent...[/]"):
            result = spawner.spawn(
                task=task,
                provider=prov_type,
                role=final_role if role else None,
                model=final_model if model else None,
                skills=list(skill) if skill else None,
                coord_task_id=coord_task_id,
                agent_name=agent_name,
            )

        _show_spawn_result(result)

    @agents.command("spawned")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def agents_spawned(home: str):
        """List all spawned sub-agents.

        \b
        Example:
            skcapstone agents spawned
        """
        from ..spawner import SubAgentSpawner

        home_path = Path(home).expanduser()
        spawner = SubAgentSpawner(home=home_path)
        results = spawner.list_spawned()

        if not results:
            console.print(
                "\n  [dim]No spawned sub-agents found.[/]\n"
                "  [dim]Spawn one:[/] [cyan]skcapstone agents spawn \"your task here\"[/]\n"
            )
            return

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Name", style="cyan")
        table.add_column("Status")
        table.add_column("Host", style="dim")
        table.add_column("PID", style="dim")
        table.add_column("Task")
        table.add_column("Deployment", style="dim", max_width=30)

        for r in results:
            status_val = r.status.value if hasattr(r.status, "value") else str(r.status)
            status_style = "green" if status_val == "running" else "yellow" if status_val == "pending" else "red"
            table.add_row(
                r.agent_name,
                f"[{status_style}]{status_val}[/]",
                r.host,
                str(r.pid) if r.pid else "\u2014",
                (r.task_description[:50] + "\u2026") if len(r.task_description) > 50 else r.task_description,
                r.deployment_id,
            )

        console.print(
            Panel(table, title=f"[bold]Spawned Sub-Agents ({len(results)})[/]", border_style="cyan")
        )

    @agents.command("kill")
    @click.argument("deployment_id")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def agents_kill(deployment_id: str, home: str):
        """Kill a spawned sub-agent by deployment ID.

        \b
        Example:
            skcapstone agents kill spawn-coder-1740000000
        """
        from ..spawner import SubAgentSpawner

        home_path = Path(home).expanduser()
        spawner = SubAgentSpawner(home=home_path)

        if spawner.kill(deployment_id):
            console.print(f"\n  [green]Killed deployment: {deployment_id}[/]\n")
        else:
            console.print(f"\n  [red]Deployment not found: {deployment_id}[/]\n")

    @agents.command("babies")
    @click.option("--verbose", "-v", is_flag=True, help="Show full descriptions and skills.")
    def agents_babies(verbose: bool):
        """List all available baby agents.

        Baby agents are the 12 pre-defined lightweight daemons of the SK*
        ecosystem. Each handles a specific operational concern like memory
        curation, trust validation, health monitoring, etc.

        \b
        Examples:
            skcapstone agents babies
            skcapstone agents babies --verbose
            skcapstone agents spawn --baby memory-curator
        """
        from ..baby_agents import list_baby_agents

        babies = list_baby_agents()

        table = Table(
            show_header=True, header_style="bold", box=None, padding=(0, 2),
        )
        table.add_column("#", style="dim", justify="right")
        table.add_column("Name", style="bold cyan")
        table.add_column("Role")
        table.add_column("Model", style="dim")

        if verbose:
            table.add_column("Skills", style="dim")
            table.add_column("Description", max_width=50)

        for idx, baby in enumerate(babies, 1):
            row = [
                str(idx),
                baby.name,
                baby.role.value,
                baby.model.value,
            ]
            if verbose:
                row.append(", ".join(baby.skills))
                row.append(baby.description[:80])
            table.add_row(*row)

        console.print()
        console.print(
            Panel(
                table,
                title=f"[bold]Baby Agents ({len(babies)})[/]",
                subtitle="[dim]skcapstone agents spawn --baby <name>[/]",
                border_style="bright_magenta",
            )
        )
        console.print()
