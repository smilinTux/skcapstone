"""GTD (Getting Things Done) inbox capture CLI commands."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click

from ._common import AGENT_HOME, console

from rich.panel import Panel
from rich.table import Table


def register_gtd_commands(main: click.Group) -> None:
    """Register the gtd command group."""

    @main.group()
    def gtd():
        """GTD inbox capture and management.

        Capture items to your GTD inbox, list inbox contents,
        and view status across all GTD lists. Data lives in
        ~/.skcapstone/coordination/gtd/ as JSON files.
        """

    @gtd.command("capture")
    @click.argument("text")
    @click.option("--source", "-s", default="manual",
                  type=click.Choice(["manual", "telegram", "email", "voice"]),
                  help="Where this item came from.")
    @click.option("--privacy", "-p", default="private",
                  type=click.Choice(["private", "team", "community", "public"]),
                  help="Privacy level.")
    @click.option("--context", "-c", default=None,
                  help="GTD context tag, e.g. @computer, @phone, @home.")
    def gtd_capture(text, source, privacy, context):
        """Capture an item to the GTD inbox.

        Example: skcapstone gtd capture "Buy milk" --context @errands
        """
        from ..mcp_tools.gtd_tools import _make_item, _load_list, _save_list

        item = _make_item(
            text=text,
            source=source,
            privacy=privacy,
            context=context,
        )
        inbox = _load_list("inbox")
        inbox.append(item)
        _save_list("inbox", inbox)

        console.print()
        console.print(Panel(
            f"[bold green]Captured![/] ID: [cyan]{item['id']}[/]\n"
            f"[dim]{item['text']}[/]\n"
            f"Source: {item['source']}  Privacy: {item['privacy']}"
            + (f"  Context: {item['context']}" if item['context'] else ""),
            title="GTD Inbox", border_style="green",
        ))
        console.print(f"  Inbox now has [bold]{len(inbox)}[/] item(s).\n")

    @gtd.command("inbox")
    @click.option("--limit", "-n", default=20, type=int,
                  help="Maximum items to show (default: 20).")
    def gtd_inbox(limit):
        """List current GTD inbox items (newest first)."""
        from ..mcp_tools.gtd_tools import _load_list

        inbox = _load_list("inbox")
        inbox.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        items = inbox[:limit]

        if not items:
            console.print("\n  [dim]Inbox is empty. Capture items with:[/]")
            console.print("  [cyan]skcapstone gtd capture \"Your item text\"[/]\n")
            return

        console.print()
        table = Table(
            show_header=True, header_style="bold",
            box=None, padding=(0, 2),
        )
        table.add_column("ID", style="cyan", max_width=14)
        table.add_column("Text", style="bold")
        table.add_column("Source", style="dim")
        table.add_column("Context", style="dim")
        table.add_column("Created", style="dim")

        for it in items:
            created = it.get("created_at", "")[:19].replace("T", " ")
            table.add_row(
                it.get("id", ""),
                it.get("text", "")[:60],
                it.get("source", ""),
                it.get("context", "") or "",
                created,
            )

        console.print(Panel(
            f"[bold]Inbox:[/] {len(inbox)} total, showing {len(items)}",
            title="GTD Inbox", border_style="bright_blue",
        ))
        console.print(table)
        console.print()

    @gtd.command("status")
    def gtd_status():
        """Summary of all GTD lists."""
        from ..mcp_tools.gtd_tools import _load_list, _GTD_LISTS

        console.print()
        total = 0
        rows = []
        for list_name in _GTD_LISTS:
            items = _load_list(list_name)
            count = len(items)
            total += count
            rows.append((list_name, count))

        table = Table(
            show_header=True, header_style="bold",
            box=None, padding=(0, 2),
        )
        table.add_column("List", style="bold")
        table.add_column("Count", style="cyan", justify="right")

        for name, count in rows:
            style = "bold yellow" if count > 0 else "dim"
            table.add_row(name, f"[{style}]{count}[/]")

        console.print(Panel(
            f"[bold]GTD Lists:[/] {total} total items across {len(rows)} lists",
            title="GTD Status", border_style="bright_blue",
        ))
        console.print(table)
        console.print()

    @gtd.command("clarify")
    @click.argument("item_id")
    @click.option("--actionable/--not-actionable", default=True,
                  help="Is this item actionable?")
    @click.option("--steps", "-s", default="single",
                  type=click.Choice(["single", "multi"]),
                  help="Single action or multi-step project.")
    @click.option("--priority", "-p", default="medium",
                  type=click.Choice(["critical", "high", "medium", "low"]),
                  help="Priority level.")
    @click.option("--energy", "-e", default="medium",
                  type=click.Choice(["high", "medium", "low"]),
                  help="Energy level required.")
    @click.option("--context", "-c", default=None,
                  help="GTD context tag, e.g. @computer, @phone, @home.")
    @click.option("--delegate-to", "-d", default=None,
                  help="Person or agent to delegate to (routes to waiting-for).")
    def gtd_clarify(item_id, actionable, steps, priority, energy, context, delegate_to):
        """Clarify an inbox item and route it to the appropriate list.

        Example: skcapstone gtd clarify abc123 --actionable --steps single --priority high --context @computer
        """
        from ..mcp_tools.gtd_tools import _handle_gtd_clarify

        result = asyncio.get_event_loop().run_until_complete(
            _handle_gtd_clarify({
                "item_id": item_id,
                "actionable": actionable,
                "steps": steps,
                "priority": priority,
                "energy": energy,
                "context": context,
                "delegate_to": delegate_to,
            })
        )
        data = json.loads(result[0].text)

        if "error" in data:
            console.print(f"\n  [bold red]Error:[/] {data['error']}\n")
            return

        console.print()
        console.print(Panel(
            f"[bold green]Clarified![/] ID: [cyan]{data['id']}[/]\n"
            f"[dim]{data['text']}[/]\n"
            f"Destination: [bold]{data['destination']}[/]  "
            f"Status: {data['status']}  Priority: {data.get('priority', '-')}  "
            f"Energy: {data.get('energy', '-')}"
            + (f"\nContext: {data['context']}" if data.get('context') else "")
            + (f"\nDelegated to: {data['delegate_to']}" if data.get('delegate_to') else ""),
            title="GTD Clarify", border_style="green",
        ))
        console.print()

    @gtd.command("move")
    @click.argument("item_id")
    @click.option("--to", "destination", required=True,
                  type=click.Choice(["next", "project", "waiting", "someday", "reference", "done"]),
                  help="Destination list.")
    def gtd_move(item_id, destination):
        """Move a GTD item to a different list.

        Example: skcapstone gtd move abc123 --to next
        """
        from ..mcp_tools.gtd_tools import _handle_gtd_move

        result = asyncio.get_event_loop().run_until_complete(
            _handle_gtd_move({
                "item_id": item_id,
                "destination": destination,
            })
        )
        data = json.loads(result[0].text)

        if "error" in data:
            console.print(f"\n  [bold red]Error:[/] {data['error']}\n")
            return

        console.print()
        console.print(Panel(
            f"[bold green]Moved![/] ID: [cyan]{data['id']}[/]\n"
            f"[dim]{data['text']}[/]\n"
            f"From: [bold]{data['from']}[/] -> To: [bold]{data['to']}[/]",
            title="GTD Move", border_style="green",
        ))
        console.print()

    @gtd.command("done")
    @click.argument("item_id")
    def gtd_done(item_id):
        """Mark a GTD item as done and archive it.

        Example: skcapstone gtd done abc123
        """
        from ..mcp_tools.gtd_tools import _handle_gtd_done

        result = asyncio.get_event_loop().run_until_complete(
            _handle_gtd_done({
                "item_id": item_id,
            })
        )
        data = json.loads(result[0].text)

        if "error" in data:
            console.print(f"\n  [bold red]Error:[/] {data['error']}\n")
            return

        console.print()
        console.print(Panel(
            f"[bold green]Done![/] ID: [cyan]{data['id']}[/]\n"
            f"[dim]{data['text']}[/]\n"
            f"Completed: {data['completed_at'][:19].replace('T', ' ')}\n"
            f"Archive now has [bold]{data['archive_count']}[/] item(s).",
            title="GTD Done", border_style="green",
        ))
        console.print()

    @gtd.command("next")
    @click.option("--context", "-c", default=None,
                  help="Filter by GTD context tag, e.g. @computer, @phone, @home.")
    @click.option("--energy", "-e", default=None,
                  type=click.Choice(["high", "medium", "low"]),
                  help="Filter by energy level required.")
    @click.option("--priority", "-p", default=None,
                  type=click.Choice(["critical", "high", "medium", "low"]),
                  help="Filter by priority level.")
    @click.option("--limit", "-n", default=10, type=int,
                  help="Maximum items to show (default: 10).")
    def gtd_next(context, energy, priority, limit):
        """View next actions filtered by context, energy, and/or priority.

        Example: skcapstone gtd next --context @computer --energy high --limit 5
        """
        from ..mcp_tools.gtd_tools import _handle_gtd_next

        result = asyncio.get_event_loop().run_until_complete(
            _handle_gtd_next({
                "context": context,
                "energy": energy,
                "priority": priority,
                "limit": limit,
            })
        )
        data = json.loads(result[0].text)

        if "error" in data:
            console.print(f"\n  [bold red]Error:[/] {data['error']}\n")
            return

        items = data.get("items", [])
        if not items:
            console.print("\n  [dim]No next actions found matching filters.[/]")
            filters = data.get("filters", {})
            active = {k: v for k, v in filters.items() if v}
            if active:
                console.print(f"  Filters: {active}\n")
            else:
                console.print("  [dim]Clarify inbox items to populate next actions.[/]\n")
            return

        console.print()
        table = Table(
            show_header=True, header_style="bold",
            box=None, padding=(0, 2),
        )
        table.add_column("ID", style="cyan", max_width=14)
        table.add_column("Text", style="bold")
        table.add_column("Priority", style="dim")
        table.add_column("Energy", style="dim")
        table.add_column("Context", style="dim")
        table.add_column("Created", style="dim")

        for it in items:
            created = it.get("created_at", "")[:19].replace("T", " ")
            pri = it.get("priority", "-") or "-"
            pri_style = {
                "critical": "bold red",
                "high": "bold yellow",
                "medium": "",
                "low": "dim",
            }.get(pri, "")
            table.add_row(
                it.get("id", ""),
                it.get("text", "")[:60],
                f"[{pri_style}]{pri}[/]" if pri_style else pri,
                it.get("energy", "-") or "-",
                it.get("context", "") or "",
                created,
            )

        console.print(Panel(
            f"[bold]Next Actions:[/] {data['total']} total, showing {data['showing']}",
            title="GTD Next Actions", border_style="bright_blue",
        ))
        console.print(table)
        console.print()

    @gtd.command("projects")
    @click.option("--status", "-s", default="all",
                  type=click.Choice(["active", "stale", "all"]),
                  help="Filter by project status (default: all).")
    @click.option("--limit", "-n", default=10, type=int,
                  help="Maximum items to show (default: 10).")
    def gtd_projects(status, limit):
        """View GTD projects with status and activity info.

        Example: skcapstone gtd projects --status active
        """
        from ..mcp_tools.gtd_tools import _handle_gtd_projects

        result = asyncio.get_event_loop().run_until_complete(
            _handle_gtd_projects({
                "status": status,
                "limit": limit,
            })
        )
        data = json.loads(result[0].text)

        if "error" in data:
            console.print(f"\n  [bold red]Error:[/] {data['error']}\n")
            return

        projects = data.get("projects", [])
        if not projects:
            console.print(f"\n  [dim]No projects found (filter: {data.get('filter', 'all')}).[/]\n")
            return

        console.print()
        table = Table(
            show_header=True, header_style="bold",
            box=None, padding=(0, 2),
        )
        table.add_column("ID", style="cyan", max_width=14)
        table.add_column("Text", style="bold")
        table.add_column("Status", style="dim")
        table.add_column("Priority", style="dim")
        table.add_column("Days Since", style="dim", justify="right")
        table.add_column("Context", style="dim")

        for proj in projects:
            status_str = proj.get("status", "")
            status_style = "bold red" if status_str == "stale" else "green"
            days = proj.get("days_since_activity")
            days_str = str(days) if days is not None else "?"
            table.add_row(
                proj.get("id", ""),
                proj.get("text", "")[:60],
                f"[{status_style}]{status_str}[/]",
                proj.get("priority", "-") or "-",
                days_str,
                proj.get("context", "") or "",
            )

        console.print(Panel(
            f"[bold]Projects:[/] {data['total']} total, showing {data['showing']} (filter: {data.get('filter', 'all')})",
            title="GTD Projects", border_style="bright_blue",
        ))
        console.print(table)
        console.print()

    @gtd.command("waiting")
    @click.option("--limit", "-n", default=10, type=int,
                  help="Maximum items to show (default: 10).")
    def gtd_waiting(limit):
        """View waiting-for items sorted by longest waiting.

        Example: skcapstone gtd waiting --limit 5
        """
        from ..mcp_tools.gtd_tools import _handle_gtd_waiting

        result = asyncio.get_event_loop().run_until_complete(
            _handle_gtd_waiting({
                "limit": limit,
            })
        )
        data = json.loads(result[0].text)

        if "error" in data:
            console.print(f"\n  [bold red]Error:[/] {data['error']}\n")
            return

        items = data.get("items", [])
        if not items:
            console.print("\n  [dim]No waiting-for items found.[/]\n")
            return

        console.print()
        table = Table(
            show_header=True, header_style="bold",
            box=None, padding=(0, 2),
        )
        table.add_column("ID", style="cyan", max_width=14)
        table.add_column("Text", style="bold")
        table.add_column("Waiting On", style="dim")
        table.add_column("Waiting Since", style="dim")
        table.add_column("Days", style="dim", justify="right")

        for it in items:
            created = it.get("created_at", "")[:10]
            days = it.get("waiting_days")
            days_str = str(days) if days is not None else "?"
            days_style = "bold red" if days is not None and days >= 14 else (
                "bold yellow" if days is not None and days >= 7 else ""
            )
            table.add_row(
                it.get("id", ""),
                it.get("text", "")[:60],
                it.get("delegate_to", "") or "?",
                created,
                f"[{days_style}]{days_str}[/]" if days_style else days_str,
            )

        console.print(Panel(
            f"[bold]Waiting For:[/] {data['total']} total, showing {data['showing']}",
            title="GTD Waiting For", border_style="bright_blue",
        ))
        console.print(table)
        console.print()

    @gtd.command("review")
    def gtd_review():
        """Generate a GTD weekly review summary.

        Shows counts per list, oldest items, longest-waiting items,
        and stale projects.
        """
        from ..mcp_tools.gtd_tools import _handle_gtd_review

        result = asyncio.get_event_loop().run_until_complete(
            _handle_gtd_review({})
        )
        data = json.loads(result[0].text)

        console.print()

        # Counts table
        counts_table = Table(
            show_header=True, header_style="bold",
            box=None, padding=(0, 2),
        )
        counts_table.add_column("List", style="bold")
        counts_table.add_column("Count", style="cyan", justify="right")

        for name, count in data.get("counts", {}).items():
            style = "bold yellow" if count > 0 else "dim"
            counts_table.add_row(name, f"[{style}]{count}[/]")

        console.print(Panel(
            f"[bold]Weekly Review[/] - {data['total']} active items  |  "
            f"{data.get('inbox_needs_clarify', 0)} inbox items need clarifying",
            title="GTD Review", border_style="bright_blue",
        ))
        console.print(counts_table)

        # Oldest items
        oldest = data.get("oldest_items", [])
        if oldest:
            console.print("\n  [bold]Oldest Items:[/]")
            for it in oldest:
                created = it.get("created_at", "")[:10]
                console.print(
                    f"    [cyan]{it['id']}[/]  [{it['list']}]  "
                    f"{it['text']}  [dim]({created})[/]"
                )

        # Longest waiting
        waiting = data.get("longest_waiting", [])
        if waiting:
            console.print("\n  [bold]Longest Waiting:[/]")
            for it in waiting:
                created = it.get("created_at", "")[:10]
                delegate = it.get("delegate_to", "?")
                console.print(
                    f"    [cyan]{it['id']}[/]  -> {delegate}  "
                    f"{it['text']}  [dim]({created})[/]"
                )

        # Stale projects
        stale = data.get("stale_projects", [])
        if stale:
            console.print("\n  [bold]Stale Projects (7+ days):[/]")
            for it in stale:
                console.print(
                    f"    [cyan]{it['id']}[/]  {it['text']}  "
                    f"[bold red]({it['days_stale']} days)[/]"
                )

        console.print()
