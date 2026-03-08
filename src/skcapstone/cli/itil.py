"""ITIL service management CLI commands."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click

from ._common import AGENT_HOME, SHARED_ROOT, console


def register_itil_commands(main: click.Group) -> None:
    """Register the itil command group."""

    @main.group()
    def itil():
        """ITIL service management — incidents, problems, changes."""

    # ── itil status ───────────────────────────────────────────────────

    @itil.command("status")
    def itil_status():
        """Show ITIL dashboard: open incidents, active problems, pending changes."""
        from ..itil import ITILManager

        mgr = ITILManager(Path(SHARED_ROOT).expanduser())
        status = mgr.get_status()

        inc = status["incidents"]
        prb = status["problems"]
        chg = status["changes"]
        kedb = status["kedb"]

        console.print(f"\n[bold]ITIL Dashboard[/bold]")
        console.print(f"  Incidents:  [red]{inc['open']}[/red] open / {inc['total']} total")
        for sev, count in inc.get("by_severity", {}).items():
            if count:
                console.print(f"    {sev}: {count}")
        console.print(f"  Problems:   [yellow]{prb['active']}[/yellow] active / {prb['total']} total")
        console.print(f"  Changes:    [blue]{chg['pending']}[/blue] pending / {chg['total']} total")
        console.print(f"  KEDB:       {kedb['total']} entries")

        if inc["open_list"]:
            console.print(f"\n[bold red]Open Incidents:[/bold red]")
            for i in inc["open_list"]:
                console.print(
                    f"  [{i['id']}] {i['severity'].upper()} {i['title']} "
                    f"({i['status']}) @{i['managed_by']}"
                )

        if chg["pending_list"]:
            console.print(f"\n[bold blue]Pending Changes:[/bold blue]")
            for c in chg["pending_list"]:
                console.print(
                    f"  [{c['id']}] {c['title']} ({c['status']}, "
                    f"{c['change_type']}) @{c['managed_by']}"
                )

        console.print()

    # ── itil incident ─────────────────────────────────────────────────

    @itil.group()
    def incident():
        """Incident management."""

    @incident.command("create")
    @click.option("--title", "-t", required=True, help="Incident title")
    @click.option(
        "--severity", "-s", default="sev3",
        type=click.Choice(["sev1", "sev2", "sev3", "sev4"]),
        help="Severity level",
    )
    @click.option("--service", multiple=True, help="Affected service(s)")
    @click.option("--impact", default="", help="Business impact")
    @click.option("--by", "managed_by", default="human", help="Managing agent")
    @click.option("--tag", multiple=True, help="Tags")
    def incident_create(title, severity, service, impact, managed_by, tag):
        """Create a new incident."""
        from ..itil import ITILManager

        mgr = ITILManager(Path(SHARED_ROOT).expanduser())
        inc = mgr.create_incident(
            title=title,
            severity=severity,
            affected_services=list(service),
            impact=impact,
            managed_by=managed_by,
            created_by=managed_by,
            tags=list(tag),
        )
        console.print(
            f"\n  [green]Created:[/green] {inc.id} — {inc.title} "
            f"({inc.severity.value}, {inc.status.value})"
        )
        if inc.gtd_item_ids:
            console.print(f"  [dim]GTD item(s): {', '.join(inc.gtd_item_ids)}[/dim]")
        console.print()

    @incident.command("list")
    @click.option(
        "--status", type=click.Choice([
            "detected", "acknowledged", "investigating",
            "escalated", "resolved", "closed",
        ]),
        help="Filter by status",
    )
    @click.option(
        "--severity",
        type=click.Choice(["sev1", "sev2", "sev3", "sev4"]),
        help="Filter by severity",
    )
    @click.option("--service", help="Filter by affected service")
    def incident_list(status, severity, service):
        """List incidents."""
        from ..itil import ITILManager

        mgr = ITILManager(Path(SHARED_ROOT).expanduser())
        incidents = mgr.list_incidents(status=status, severity=severity, service=service)

        if not incidents:
            console.print("\n  [dim]No incidents found[/dim]\n")
            return

        console.print(f"\n[bold]Incidents ({len(incidents)}):[/bold]")
        for i in incidents:
            sev = i.severity.value.upper()
            console.print(
                f"  [{i.id}] {sev} {i.title} ({i.status.value}) @{i.managed_by}"
            )
        console.print()

    @incident.command("update")
    @click.argument("incident_id")
    @click.option("--agent", default="human", help="Agent making the update")
    @click.option(
        "--status", "new_status",
        type=click.Choice([
            "acknowledged", "investigating", "escalated", "resolved", "closed",
        ]),
        help="New status",
    )
    @click.option(
        "--severity",
        type=click.Choice(["sev1", "sev2", "sev3", "sev4"]),
        help="New severity",
    )
    @click.option("--note", default="", help="Timeline note")
    @click.option("--resolution", default=None, help="Resolution summary")
    def incident_update(incident_id, agent, new_status, severity, note, resolution):
        """Update an incident status or metadata."""
        from ..itil import ITILManager

        mgr = ITILManager(Path(SHARED_ROOT).expanduser())
        try:
            inc = mgr.update_incident(
                incident_id=incident_id,
                agent=agent,
                new_status=new_status,
                severity=severity,
                note=note,
                resolution_summary=resolution,
            )
            console.print(
                f"\n  [green]Updated:[/green] {inc.id} -> {inc.status.value} "
                f"({inc.severity.value})\n"
            )
        except ValueError as exc:
            console.print(f"\n  [red]Error:[/red] {exc}\n")

    # ── itil problem ──────────────────────────────────────────────────

    @itil.group()
    def problem():
        """Problem management."""

    @problem.command("create")
    @click.option("--title", "-t", required=True, help="Problem title")
    @click.option("--by", "managed_by", default="human", help="Managing agent")
    @click.option("--incident", "incident_ids", multiple=True, help="Related incident ID(s)")
    @click.option("--workaround", default="", help="Known workaround")
    @click.option("--tag", multiple=True, help="Tags")
    def problem_create(title, managed_by, incident_ids, workaround, tag):
        """Create a new problem record."""
        from ..itil import ITILManager

        mgr = ITILManager(Path(SHARED_ROOT).expanduser())
        prb = mgr.create_problem(
            title=title,
            managed_by=managed_by,
            created_by=managed_by,
            related_incident_ids=list(incident_ids),
            workaround=workaround,
            tags=list(tag),
        )
        console.print(
            f"\n  [green]Created:[/green] {prb.id} — {prb.title} ({prb.status.value})\n"
        )

    @problem.command("list")
    @click.option(
        "--status",
        type=click.Choice(["identified", "analyzing", "known_error", "resolved"]),
        help="Filter by status",
    )
    def problem_list(status):
        """List problems."""
        from ..itil import ITILManager

        mgr = ITILManager(Path(SHARED_ROOT).expanduser())
        problems = mgr.list_problems(status=status)

        if not problems:
            console.print("\n  [dim]No problems found[/dim]\n")
            return

        console.print(f"\n[bold]Problems ({len(problems)}):[/bold]")
        for p in problems:
            console.print(
                f"  [{p.id}] {p.title} ({p.status.value}) @{p.managed_by}"
            )
        console.print()

    @problem.command("update")
    @click.argument("problem_id")
    @click.option("--agent", default="human", help="Agent making the update")
    @click.option(
        "--status", "new_status",
        type=click.Choice(["analyzing", "known_error", "resolved"]),
        help="New status",
    )
    @click.option("--root-cause", default=None, help="Root cause description")
    @click.option("--workaround", default=None, help="Workaround")
    @click.option("--note", default="", help="Timeline note")
    @click.option("--create-kedb", is_flag=True, help="Create KEDB entry")
    def problem_update(problem_id, agent, new_status, root_cause, workaround, note, create_kedb):
        """Update a problem record."""
        from ..itil import ITILManager

        mgr = ITILManager(Path(SHARED_ROOT).expanduser())
        try:
            prb = mgr.update_problem(
                problem_id=problem_id,
                agent=agent,
                new_status=new_status,
                root_cause=root_cause,
                workaround=workaround,
                note=note,
                create_kedb=create_kedb,
            )
            console.print(
                f"\n  [green]Updated:[/green] {prb.id} -> {prb.status.value}\n"
            )
            if prb.kedb_id:
                console.print(f"  [dim]KEDB entry: {prb.kedb_id}[/dim]\n")
        except ValueError as exc:
            console.print(f"\n  [red]Error:[/red] {exc}\n")

    # ── itil change ───────────────────────────────────────────────────

    @itil.group()
    def change():
        """Change management (RFC)."""

    @change.command("propose")
    @click.option("--title", "-t", required=True, help="Change title")
    @click.option(
        "--type", "change_type", default="normal",
        type=click.Choice(["standard", "normal", "emergency"]),
        help="Change type",
    )
    @click.option(
        "--risk", default="medium",
        type=click.Choice(["low", "medium", "high"]),
        help="Risk level",
    )
    @click.option("--rollback", default="", help="Rollback plan")
    @click.option("--test-plan", default="", help="Test plan")
    @click.option("--by", "managed_by", default="human", help="Managing agent")
    @click.option("--implementer", default=None, help="Implementing agent")
    @click.option("--problem", "related_problem_id", default=None, help="Related problem ID")
    @click.option("--tag", multiple=True, help="Tags")
    def change_propose(title, change_type, risk, rollback, test_plan,
                       managed_by, implementer, related_problem_id, tag):
        """Propose a new change (RFC)."""
        from ..itil import ITILManager

        mgr = ITILManager(Path(SHARED_ROOT).expanduser())
        chg = mgr.propose_change(
            title=title,
            change_type=change_type,
            risk=risk,
            rollback_plan=rollback,
            test_plan=test_plan,
            managed_by=managed_by,
            created_by=managed_by,
            implementer=implementer,
            related_problem_id=related_problem_id,
            tags=list(tag),
        )
        console.print(
            f"\n  [green]Proposed:[/green] {chg.id} — {chg.title} "
            f"({chg.change_type.value}, {chg.status.value})\n"
        )

    @change.command("list")
    @click.option(
        "--status",
        type=click.Choice([
            "proposed", "reviewing", "approved", "rejected",
            "implementing", "deployed", "verified", "failed", "closed",
        ]),
        help="Filter by status",
    )
    def change_list(status):
        """List changes."""
        from ..itil import ITILManager

        mgr = ITILManager(Path(SHARED_ROOT).expanduser())
        changes = mgr.list_changes(status=status)

        if not changes:
            console.print("\n  [dim]No changes found[/dim]\n")
            return

        console.print(f"\n[bold]Changes ({len(changes)}):[/bold]")
        for c in changes:
            console.print(
                f"  [{c.id}] {c.title} ({c.status.value}, "
                f"{c.change_type.value}) @{c.managed_by}"
            )
        console.print()

    @change.command("update")
    @click.argument("change_id")
    @click.option("--agent", default="human", help="Agent making the update")
    @click.option(
        "--status", "new_status",
        type=click.Choice([
            "reviewing", "approved", "rejected", "implementing",
            "deployed", "verified", "failed", "closed",
        ]),
        help="New status",
    )
    @click.option("--note", default="", help="Timeline note")
    def change_update(change_id, agent, new_status, note):
        """Update a change status."""
        from ..itil import ITILManager

        mgr = ITILManager(Path(SHARED_ROOT).expanduser())
        try:
            chg = mgr.update_change(
                change_id=change_id,
                agent=agent,
                new_status=new_status,
                note=note,
            )
            console.print(
                f"\n  [green]Updated:[/green] {chg.id} -> {chg.status.value}\n"
            )
        except ValueError as exc:
            console.print(f"\n  [red]Error:[/red] {exc}\n")

    # ── itil cab ──────────────────────────────────────────────────────

    @itil.group()
    def cab():
        """Change Advisory Board voting."""

    @cab.command("vote")
    @click.argument("change_id")
    @click.option("--agent", default="human", help="Voting agent")
    @click.option(
        "--decision", default="approved",
        type=click.Choice(["approved", "rejected", "abstain"]),
        help="Vote decision",
    )
    @click.option("--conditions", default="", help="Approval conditions")
    def cab_vote(change_id, agent, decision, conditions):
        """Submit a CAB vote for a change."""
        from ..itil import ITILManager

        mgr = ITILManager(Path(SHARED_ROOT).expanduser())
        vote = mgr.submit_cab_vote(
            change_id=change_id,
            agent=agent,
            decision=decision,
            conditions=conditions,
        )
        console.print(
            f"\n  [green]Voted:[/green] {vote.agent} -> {vote.decision.value} "
            f"on {vote.change_id}\n"
        )

    # ── itil kedb ─────────────────────────────────────────────────────

    @itil.group()
    def kedb():
        """Known Error Database."""

    @kedb.command("search")
    @click.argument("query")
    def kedb_search(query):
        """Search the Known Error Database."""
        from ..itil import ITILManager

        mgr = ITILManager(Path(SHARED_ROOT).expanduser())
        results = mgr.search_kedb(query)

        if not results:
            console.print(f"\n  [dim]No KEDB entries matching '{query}'[/dim]\n")
            return

        console.print(f"\n[bold]KEDB Results ({len(results)}):[/bold]")
        for e in results:
            console.print(f"  [{e.id}] {e.title}")
            if e.workaround:
                console.print(f"    [dim]Workaround: {e.workaround[:100]}[/dim]")
            if e.root_cause:
                console.print(f"    [dim]Root cause: {e.root_cause[:100]}[/dim]")
        console.print()

    # ── itil board ────────────────────────────────────────────────────

    @itil.command("board")
    def itil_board():
        """Generate ITIL-BOARD.md overview."""
        from ..itil import ITILManager

        mgr = ITILManager(Path(SHARED_ROOT).expanduser())
        path = mgr.write_board_md()
        console.print(f"\n  [green]Generated:[/green] {path}\n")
