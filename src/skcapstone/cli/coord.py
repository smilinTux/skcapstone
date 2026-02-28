"""Coordination board commands: status, create, claim, complete, board, changelog, briefing."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ._common import AGENT_HOME, console

from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def register_coord_commands(main: click.Group) -> None:
    """Register the coord command group."""

    @main.group()
    def coord():
        """Multi-agent coordination board.

        Create tasks, claim work, and track progress across
        agents. All data lives in ~/.skcapstone/coordination/
        and syncs via Syncthing. Conflict-free by design.
        """

    @coord.command("status")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def coord_status(home):
        """Show the coordination board overview."""
        from ..coordination import Board

        home_path = Path(home).expanduser()
        board = Board(home_path)
        views = board.get_task_views()
        agents = board.load_agents()

        if not views and not agents:
            console.print("\n  [dim]Board is empty. Create tasks with:[/]")
            console.print("  [cyan]skcapstone coord create --title 'My Task'[/]\n")
            return

        open_count = sum(1 for v in views if v.status.value == "open")
        progress_count = sum(1 for v in views if v.status.value == "in_progress")
        claimed_count = sum(1 for v in views if v.status.value == "claimed")
        done_count = sum(1 for v in views if v.status.value == "done")

        console.print()
        console.print(Panel(
            f"[bold]Tasks:[/] {len(views)} total  "
            f"[green]{open_count} open[/]  "
            f"[cyan]{claimed_count} claimed[/]  "
            f"[yellow]{progress_count} in progress[/]  "
            f"[dim]{done_count} done[/]",
            title="Coordination Board", border_style="bright_blue",
        ))

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("ID", style="cyan", max_width=10)
        table.add_column("Title", style="bold")
        table.add_column("Priority")
        table.add_column("Status")
        table.add_column("Assignee", style="dim")
        table.add_column("Tags", style="dim")

        priority_colors = {"critical": "bold red", "high": "red", "medium": "yellow", "low": "dim"}
        status_colors = {"open": "green", "claimed": "cyan", "in_progress": "yellow", "done": "dim", "blocked": "red"}

        for v in views:
            if v.status.value == "done":
                continue
            t = v.task
            p_style = priority_colors.get(t.priority.value, "dim")
            s_style = status_colors.get(v.status.value, "dim")
            table.add_row(t.id, t.title, Text(t.priority.value.upper(), style=p_style),
                          Text(v.status.value.upper(), style=s_style), v.claimed_by or "", ", ".join(t.tags))

        console.print(table)

        if agents:
            console.print()
            for ag in agents:
                icon = {"active": "[green]ACTIVE[/]", "idle": "[yellow]IDLE[/]"}.get(ag.state.value, "[dim]OFFLINE[/]")
                current = f" -> [cyan]{ag.current_task}[/]" if ag.current_task else ""
                console.print(f"  {icon} [bold]{ag.agent}[/]{current}")
        console.print()

    @coord.command("create")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--title", required=True, help="Task title.")
    @click.option("--desc", default="", help="Task description.")
    @click.option("--priority", type=click.Choice(["critical", "high", "medium", "low"]), default="medium")
    @click.option("--tag", multiple=True, help="Tags (repeatable).")
    @click.option("--by", default="human", help="Creator name.")
    @click.option("--criteria", multiple=True, help="Acceptance criteria (repeatable).")
    @click.option("--dep", multiple=True, help="Dependency task IDs (repeatable).")
    def coord_create(home, title, desc, priority, tag, by, criteria, dep):
        """Create a new task on the board."""
        from ..coordination import Board, Task, TaskPriority

        home_path = Path(home).expanduser()
        board = Board(home_path)
        task = Task(title=title, description=desc, priority=TaskPriority(priority),
                    tags=list(tag), created_by=by, acceptance_criteria=list(criteria), dependencies=list(dep))
        path = board.create_task(task)
        console.print(f"\n  [green]Created:[/] [{task.id}] {task.title}")
        console.print(f"  [dim]{path}[/]\n")

    @coord.command("claim")
    @click.argument("task_id")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--agent", required=True, help="Agent name claiming the task.")
    def coord_claim(task_id, home, agent):
        """Claim a task for an agent."""
        from ..coordination import Board

        home_path = Path(home).expanduser()
        board = Board(home_path)
        try:
            ag = board.claim_task(agent, task_id)
            console.print(f"\n  [green]Claimed:[/] [{task_id}] by [bold]{ag.agent}[/]\n")
        except ValueError as e:
            console.print(f"\n  [red]Error:[/] {e}\n")
            sys.exit(1)

    @coord.command("complete")
    @click.argument("task_id")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--agent", required=True, help="Agent name completing the task.")
    def coord_complete(task_id, home, agent):
        """Mark a task as completed."""
        from ..coordination import Board

        home_path = Path(home).expanduser()
        board = Board(home_path)
        ag = board.complete_task(agent, task_id)
        console.print(f"\n  [green]Completed:[/] [{task_id}] by [bold]{ag.agent}[/]\n")

    @coord.command("board")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def coord_board(home):
        """Generate and display the BOARD.md overview."""
        from ..coordination import Board

        home_path = Path(home).expanduser()
        board = Board(home_path)
        path = board.write_board_md()
        md = board.generate_board_md()
        console.print(md)
        console.print(f"\n  [dim]Written to {path}[/]\n")

    @coord.command("changelog")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--output", "-o", default=None, type=click.Path(), help="Output file path.")
    def coord_changelog(home, output):
        """Generate CHANGELOG.md from completed board tasks."""
        from ..changelog import generate_changelog, write_changelog

        home_path = Path(home).expanduser()
        out_path = Path(output) if output else None
        path = write_changelog(home_path, out_path)

        content = generate_changelog(home_path)
        console.print(content[:3000])
        if len(content) > 3000:
            console.print(f"\n  [dim]... ({len(content)} chars total)[/]")
        console.print(f"\n  [green]Written to {path}[/]\n")

    @coord.command("briefing")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
    def coord_briefing(home, fmt):
        """Print the full coordination protocol for any AI agent."""
        from ..coordination import get_briefing_text, get_briefing_json

        home_path = Path(home).expanduser()
        if fmt == "json":
            click.echo(get_briefing_json(home_path))
        else:
            click.echo(get_briefing_text(home_path))
