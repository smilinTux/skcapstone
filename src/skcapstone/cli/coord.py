"""Coordination board commands: status, create, claim, complete, board, changelog, briefing."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ._common import AGENT_HOME, console
from ._validators import validate_agent_name, validate_task_id

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

        validate_agent_name(by)
        for d in dep:
            validate_task_id(d)

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

        validate_task_id(task_id)
        validate_agent_name(agent)

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

        validate_task_id(task_id)
        validate_agent_name(agent)

        home_path = Path(home).expanduser()
        board = Board(home_path)
        ag = board.complete_task(agent, task_id)
        # board.complete_task() automatically mints Joules via _mint_joules_for_task
        console.print(f"\n  [green]Completed:[/] [{task_id}] by [bold]{ag.agent}[/]\n")

    @coord.command("score")
    @click.argument("task_id")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--round", "round_", required=True, type=int, help="Grading round number.")
    @click.option("--score", required=True, type=int, help="Score value (rubric 1-5).")
    @click.option("--notes", default="", help="Grader notes.")
    @click.option("--harness", default="", help="Harness / grader identity.")
    @click.option("--phase", default=None, help="Autopilot phase label.")
    @click.option("--ref", default=None, help="PR URL (http*) or artifact ref.")
    def coord_score(task_id, home, round_, score, notes, harness, phase, ref):
        """Record an autopilot grade on a task (meta.autopilot.scores)."""
        from ..coordination import Board

        validate_task_id(task_id)
        home_path = Path(home).expanduser()
        board = Board(home_path)
        try:
            path = board.score_task(task_id, round=round_, score=score, notes=notes,
                                    harness=harness, phase=phase, ref=ref)
        except FileNotFoundError as e:
            console.print(f"\n  [red]Error:[/] {e}\n")
            sys.exit(1)
        console.print(f"\n  [green]Scored:[/] [{task_id}] round {round_} = {score}")
        console.print(f"  [dim]{path}[/]\n")

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

    @coord.command("kanban")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--html", "html_out", default=None, type=click.Path(),
                  help="Write the visual kanban board to this HTML file.")
    @click.option("--json", "as_json", is_flag=True, default=False,
                  help="Emit the grid as JSON instead of a text summary.")
    def coord_kanban(home, html_out, as_json):
        """Unified kanban board over coord tasks and ITIL tickets.

        Columns are the shared lifecycle (backlog, ready, doing, review, done);
        swimlanes are the card kind (feature, bug, security, expedite, change,
        problem). Reads both stores read-only.
        """
        import json as _json

        from ..card import COLUMN_ORDER, LANE_ORDER, KanbanBoard, render_html

        home_path = Path(home).expanduser()
        kb = KanbanBoard(home_path)

        if html_out:
            out = Path(html_out).expanduser()
            out.write_text(render_html(kb), encoding="utf-8")
            console.print(f"\n  [green]Kanban board written to {out}[/]\n")
            return

        grid = kb.grid()
        if as_json:
            payload = {
                lane: {col: [c.model_dump() for c in grid[lane][col]] for col in COLUMN_ORDER}
                for lane in LANE_ORDER
            }
            click.echo(_json.dumps(payload, indent=2))
            return

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Swimlane", style="bold")
        for col in COLUMN_ORDER:
            table.add_column(col.capitalize(), justify="right")
        for lane in LANE_ORDER:
            counts = [len(grid[lane][col]) for col in COLUMN_ORDER]
            if not any(counts):
                continue
            table.add_row(lane, *[str(n) if n else "[dim]-[/]" for n in counts])
        console.print()
        console.print(Panel(table, title="Kanban (columns x swimlanes)",
                            border_style="bright_blue"))
        console.print("  [dim]Full board: [cyan]coord kanban --html board.html[/][/]\n")

    @coord.command("archive-done")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--days", default=14, type=int, help="Archive done tasks older than N days.")
    @click.option("--dry-run", is_flag=True, default=False,
                  help="Show what would be archived without writing.")
    def coord_archive_done(home, days, dry_run):
        """Age done tasks off the active board (default: older than 14 days)."""
        from ..coordination import Board

        home_path = Path(home).expanduser()
        board = Board(home_path)
        ids = board.archive_done_tasks(older_than_days=days, dry_run=dry_run)
        verb = "Would archive" if dry_run else "Archived"
        console.print(f"\n  [green]{verb} {len(ids)} done task(s) older than {days}d.[/]\n")

    @coord.command("age-backlog")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--days", default=90, type=int,
                  help="Archive unclaimed open tasks older than N days.")
    @click.option("--dry-run", is_flag=True, default=False,
                  help="Show what would be archived without writing.")
    def coord_age_backlog(home, days, dry_run):
        """Archive ancient unclaimed open tasks (default: older than 90 days)."""
        from ..coordination import Board

        home_path = Path(home).expanduser()
        board = Board(home_path)
        ids = board.age_stale_open(older_than_days=days, dry_run=dry_run)
        verb = "Would archive" if dry_run else "Archived"
        console.print(f"\n  [green]{verb} {len(ids)} stale open task(s) older than {days}d.[/]\n")

    @coord.command("move")
    @click.argument("task_id")
    @click.argument("column",
                    type=click.Choice(["backlog", "ready", "doing", "review", "done"]))
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--order", default=None, type=int, help="Position within the column.")
    @click.option("--agent", default=None, help="Writer name (defaults to host).")
    def coord_move(task_id, column, home, order, agent):
        """Move a card to a kanban column (backlog/ready/doing/review/done)."""
        from ..card import CardEvent, CardEventLog

        home_path = Path(home).expanduser()
        event = CardEvent(card_id=task_id, action="move", column=column, order=order,
                          writer=agent or "")
        CardEventLog(home_path).append(event)
        pos = f" at order {order}" if order is not None else ""
        console.print(f"\n  [green]Moved {task_id} to '{column}'{pos}.[/]\n")

    @coord.command("label")
    @click.argument("task_id")
    @click.argument("label")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--remove", is_flag=True, default=False, help="Remove the label instead.")
    @click.option("--agent", default=None, help="Writer name (defaults to host).")
    def coord_label(task_id, label, home, remove, agent):
        """Add (or remove) a label on a card."""
        from ..card import CardEvent, CardEventLog

        home_path = Path(home).expanduser()
        action = "remove_label" if remove else "add_label"
        CardEventLog(home_path).append(
            CardEvent(card_id=task_id, action=action, label=label, writer=agent or "")
        )
        verb = "Removed" if remove else "Added"
        console.print(f"\n  [green]{verb} label '{label}' on {task_id}.[/]\n")

    @coord.command("link")
    @click.argument("task_id")
    @click.argument("key")
    @click.argument("value")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--agent", default=None, help="Writer name (defaults to host).")
    def coord_link(task_id, key, value, home, agent):
        """Attach a link (pr/commit/doc/...) to a card."""
        from ..card import CardEvent, CardEventLog

        home_path = Path(home).expanduser()
        CardEventLog(home_path).append(
            CardEvent(card_id=task_id, action="link", link_key=key, link_value=value,
                      writer=agent or "")
        )
        console.print(f"\n  [green]Linked {task_id}: {key} = {value}.[/]\n")

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
