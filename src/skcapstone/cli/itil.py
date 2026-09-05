"""ITIL service management CLI commands."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

from ..key_io import read_armored_public_key
from ._common import SHARED_ROOT, console

# Default grace window for an ASAP schedule (CM P1.2 / design doc section
# 4.3), mirrored from mcp_tools/itil_tools.py so the CLI and MCP surfaces
# compute the identical window for the identical input.
_SCHEDULE_GRACE_HOURS = 4


def _human_profile():
    """Load the local CapAuth human profile and its key paths."""
    from capauth import resolve_capauth_home
    from capauth.profile import load_profile

    home = resolve_capauth_home()
    profile = load_profile(base_dir=home)
    if str(profile.entity.entity_type).lower() not in {"human", "entitytype.human"}:
        raise click.ClickException("the active CapAuth profile is not human")
    return home, profile


def _verified_cab_authorization(
    path: Path, change_id: str, decision: str, target: str, scope: str
):
    """Verify and single-use consume one Chef/owner CAB authorization."""
    from capauth.crypto import get_backend

    from ..operator_authorization import (
        AuthorizationError,
        consume_authorization,
        load_authorization,
        verify_authorization,
    )

    home, profile = _human_profile()
    pub_key_path = home / "identity" / "public.asc"
    public_armor = read_armored_public_key(pub_key_path)
    if not public_armor:
        raise click.ClickException(f"cannot read a usable public key from {pub_key_path}")
    envelope = load_authorization(path)
    if envelope.issuer_fingerprint != profile.key_info.fingerprint:
        raise click.ClickException("authorization signer does not match the human profile")
    backend = get_backend(profile.crypto_backend)
    try:
        verify_authorization(
            envelope,
            public_key_armor=public_armor,
            verifier=backend.verify,
            expected_action=f"itil.cab.vote.{decision}",
            expected_target=target,
            expected_change_id=change_id,
            expected_scope=scope,
        )
        consume_authorization(envelope, home / "operator" / "used-authorizations")
    except AuthorizationError as exc:
        raise click.ClickException(str(exc)) from exc
    return envelope


def _resolve_cab_vote_subject() -> str | None:
    """Resolve the caller's capauth-authenticated identity for CAB voting.

    CR change-mgmt P1.4: the CLI twin of
    ``mcp_tools/itil_tools.py::_resolve_authenticated_subject`` - same
    canonical resolver, same fall-back-to-None shape (never raises), so a
    dev/legacy environment without capauth installed keeps
    ``submit_cab_vote``'s pre-existing free-text behavior.
    """
    try:
        from capauth import resolve_agent_identity

        ident = resolve_agent_identity()
        return ident.agent or None
    except Exception:  # noqa: BLE001 - resolver failure must never crash a vote
        return None


def _schedule_window(asap: bool, at: str | None) -> tuple[str, str]:
    """Compute (window_start, window_end) for a schedule event.

    CLI twin of ``mcp_tools/itil_tools.py::_schedule_window``.
    """
    if at:
        start = datetime.fromisoformat(at.replace("Z", "+00:00"))
    else:
        start = datetime.now(timezone.utc)
    end = start + timedelta(hours=_SCHEDULE_GRACE_HOURS)
    return start.isoformat(), end.isoformat()


def register_itil_commands(main: click.Group) -> None:
    """Register the itil command group."""

    @main.group()
    def itil():
        """ITIL service management - incidents, problems, changes."""

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

        console.print("\n[bold]ITIL Dashboard[/bold]")
        console.print(f"  Incidents:  [red]{inc['open']}[/red] open / {inc['total']} total")
        for sev, count in inc.get("by_severity", {}).items():
            if count:
                console.print(f"    {sev}: {count}")
        console.print(
            f"  Problems:   [yellow]{prb['active']}[/yellow] active / {prb['total']} total"
        )
        console.print(
            f"  Changes:    [blue]{chg['pending']}[/blue] pending / {chg['total']} total"
        )
        console.print(f"  KEDB:       {kedb['total']} entries")

        if inc["open_list"]:
            console.print("\n[bold red]Open Incidents:[/bold red]")
            for i in inc["open_list"]:
                console.print(
                    f"  [{i['id']}] {i['severity'].upper()} {i['title']} "
                    f"({i['status']}) @{i['managed_by']}"
                )

        if chg["pending_list"]:
            console.print("\n[bold blue]Pending Changes:[/bold blue]")
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
        "--severity",
        "-s",
        default="sev3",
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
            f"\n  [green]Created:[/green] {inc.id} - {inc.title} "
            f"({inc.severity.value}, {inc.status.value})"
        )
        if inc.gtd_item_ids:
            console.print(f"  [dim]GTD item(s): {', '.join(inc.gtd_item_ids)}[/dim]")
        console.print()

    @incident.command("list")
    @click.option(
        "--status",
        type=click.Choice(
            [
                "detected",
                "acknowledged",
                "investigating",
                "escalated",
                "resolved",
                "closed",
            ]
        ),
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
            console.print(f"  [{i.id}] {sev} {i.title} ({i.status.value}) @{i.managed_by}")
        console.print()

    @incident.command("update")
    @click.argument("incident_id")
    @click.option("--agent", default="human", help="Agent making the update")
    @click.option(
        "--status",
        "new_status",
        type=click.Choice(
            [
                "acknowledged",
                "investigating",
                "escalated",
                "resolved",
                "closed",
            ]
        ),
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
            # An illegal transition is recorded as a conflicted event and folded
            # away, leaving status unchanged. Without this check the command
            # prints a green "Updated:" line for a no-op, which reads as success.
            if new_status and inc.status.value != new_status:
                from ..itil import _INCIDENT_TRANSITIONS

                allowed = sorted(_INCIDENT_TRANSITIONS.get(inc.status.value, set()))
                console.print(
                    f"\n  [yellow]No change:[/yellow] {inc.id} is still "
                    f"[bold]{inc.status.value}[/bold] - "
                    f"{inc.status.value} -> {new_status} is not a legal transition.\n"
                    f"  Allowed from {inc.status.value}: "
                    f"{', '.join(allowed) if allowed else '(terminal state)'}\n"
                )
                return
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
        console.print(f"\n  [green]Created:[/green] {prb.id} - {prb.title} ({prb.status.value})\n")

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
            console.print(f"  [{p.id}] {p.title} ({p.status.value}) @{p.managed_by}")
        console.print()

    @problem.command("update")
    @click.argument("problem_id")
    @click.option("--agent", default="human", help="Agent making the update")
    @click.option(
        "--status",
        "new_status",
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
            console.print(f"\n  [green]Updated:[/green] {prb.id} -> {prb.status.value}\n")
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
        "--type",
        "change_type",
        default="normal",
        type=click.Choice(["standard", "normal", "emergency"]),
        help="Change type",
    )
    @click.option(
        "--risk",
        default="medium",
        type=click.Choice(["low", "medium", "high"]),
        help="Risk level",
    )
    @click.option("--rollback", default="", help="Rollback plan")
    @click.option("--test-plan", default="", help="Test plan")
    @click.option("--by", "managed_by", default="human", help="Managing agent")
    @click.option("--implementer", default=None, help="Implementing agent")
    @click.option("--problem", "related_problem_id", default=None, help="Related problem ID")
    @click.option("--tag", multiple=True, help="Tags")
    def change_propose(
        title,
        change_type,
        risk,
        rollback,
        test_plan,
        managed_by,
        implementer,
        related_problem_id,
        tag,
    ):
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
            f"\n  [green]Proposed:[/green] {chg.id} - {chg.title} "
            f"({chg.change_type.value}, {chg.status.value})\n"
        )

    @change.command("list")
    @click.option(
        "--status",
        type=click.Choice(
            [
                "proposed",
                "reviewing",
                "approved",
                "rejected",
                "implementing",
                "deployed",
                "verified",
                "failed",
                "closed",
            ]
        ),
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
                f"  [{c.id}] {c.title} ({c.status.value}, {c.change_type.value}) @{c.managed_by}"
            )
        console.print()

    @change.command("update")
    @click.argument("change_id")
    @click.option("--agent", default="human", help="Agent making the update")
    @click.option(
        "--status",
        "new_status",
        type=click.Choice(
            [
                "reviewing",
                "approved",
                "rejected",
                "implementing",
                "deployed",
                "verified",
                "failed",
                "closed",
            ]
        ),
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
            console.print(f"\n  [green]Updated:[/green] {chg.id} -> {chg.status.value}\n")
        except ValueError as exc:
            console.print(f"\n  [red]Error:[/red] {exc}\n")

    @change.command("preflight")
    @click.argument("change_id")
    @click.option("--agent", default="executor", help="Agent/system running the preflight")
    @click.option(
        "--passed/--failed",
        "passed",
        default=None,
        required=True,
        help="Whether the execution preflight passed",
    )
    @click.option("--reason", required=True, help="Outcome or failure reason")
    def change_preflight(change_id, agent, passed, reason):
        """Record the fail-closed execution preflight without mutating the target."""
        from ..itil import ITILManager

        if passed is None:
            raise click.ClickException("pass --passed or --failed")
        mgr = ITILManager(Path(SHARED_ROOT).expanduser())
        try:
            chg = mgr.record_change_preflight(
                change_id,
                agent,
                passed=passed,
                reason=reason,
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        verdict = "PASS" if passed else "FAIL"
        console.print(f"\n  [bold]{verdict}[/bold] preflight recorded for {chg.id}\n")

    @change.command("validate")
    @click.argument("change_id")
    @click.option("--agent", default="ci", help="Agent/system attaching the verdict")
    @click.option(
        "--passed/--failed",
        "passed",
        default=None,
        required=True,
        help="Whether the checks passed",
    )
    @click.option("--head-sha", default=None, help="Git SHA the checks ran against")
    @click.option("--url", default=None, help="URL to the CI run / PR checks")
    @click.option("--summary", default=None, help="Free-text summary of the verdict")
    def change_validate(change_id, agent, passed, head_sha, url, summary):
        """Attach a CI validation verdict to a change's draft PR.

        A passing verdict while the change is still 'proposed' auto-advances
        it to 'reviewing' (ready for CAB); a failing verdict leaves status
        unchanged.
        """
        from ..itil import Change, ITILManager

        # Enforce the verdict explicitly. click's `required=True` on a
        # --passed/--failed flag PAIR is not honoured consistently across click
        # versions: it raised locally and silently passed None in CI, so the
        # command recorded a verdict of "neither" and exited 0. A validation
        # verdict that records nothing while reporting success is worse than a
        # refusal, so decide it here rather than depending on the library.
        if passed is None:
            console.print("\n  [red]Error:[/red] pass --passed or --failed to record a verdict\n")
            raise SystemExit(2)

        mgr = ITILManager(Path(SHARED_ROOT).expanduser())
        rid = mgr._resolve_id(mgr.changes_dir, change_id)
        if mgr._load_core(mgr.changes_dir, rid) is None:
            console.print(f"\n  [red]Error:[/red] Change {change_id} not found\n")
            return

        mgr._append_event(
            mgr.changes_dir,
            rid,
            agent,
            "validation",
            passed=passed,
            head_sha=head_sha,
            url=url,
            summary=summary,
        )
        chg = mgr._fold_record(mgr.changes_dir, rid, Change)
        verdict = "[green]PASS[/green]" if passed else "[red]FAIL[/red]"
        console.print(f"\n  {verdict} attached to {chg.id} -> status: {chg.status.value}\n")

    @change.command("schedule")
    @click.argument("change_id")
    @click.option("--agent", default="human", help="Agent/operator scheduling the change")
    @click.option("--asap", is_flag=True, help="Schedule ASAP (now + grace window)")
    @click.option("--at", default=None, help="ISO 8601 window start (mutually excl. w/ --asap)")
    @click.option(
        "--deploy-mode",
        default="confirm",
        type=click.Choice(["confirm", "auto"]),
        help="Deploy mode (default: confirm - requires a human arm)",
    )
    @click.option("--note", default="", help="Timeline note")
    def change_schedule(change_id, agent, asap, at, deploy_mode, note):
        """Schedule an APPROVED change for deployment: ASAP or a window start.

        Valid only while the change is 'approved' (fold-enforced); scheduling
        a change that is not approved is refused with no state change.
        """
        from ..itil import Change, ITILManager

        if asap and at:
            console.print("\n  [red]Error:[/red] --asap and --at are mutually exclusive\n")
            return
        if not asap and not at:
            console.print("\n  [red]Error:[/red] one of --asap or --at is required\n")
            return

        mgr = ITILManager(Path(SHARED_ROOT).expanduser())
        rid = mgr._resolve_id(mgr.changes_dir, change_id)
        if mgr._load_core(mgr.changes_dir, rid) is None:
            console.print(f"\n  [red]Error:[/red] Change {change_id} not found\n")
            return

        window_start, window_end = _schedule_window(asap, at)
        mgr._append_event(
            mgr.changes_dir,
            rid,
            agent,
            "schedule",
            window_start=window_start,
            window_end=window_end,
            asap=asap,
            deploy_mode=deploy_mode,
            note=note,
        )
        chg = mgr._fold_record(mgr.changes_dir, rid, Change)
        if chg.status.value != "scheduled":
            console.print(
                f"\n  [yellow]Refused:[/yellow] {chg.id} is still [bold]{chg.status.value}[/bold] "
                "- schedule is only valid while the change is 'approved'.\n"
            )
            return
        console.print(
            f"\n  [green]Scheduled:[/green] {chg.id} -> {window_start} .. {window_end} "
            f"(deploy_mode={deploy_mode})\n"
        )

    @change.command("unschedule")
    @click.argument("change_id")
    @click.option("--agent", default="human", help="Agent/operator unscheduling the change")
    @click.option("--note", default="", help="Timeline note")
    def change_unschedule(change_id, agent, note):
        """Unschedule a change: scheduled -> approved, clears the window."""
        from ..itil import Change, ITILManager

        mgr = ITILManager(Path(SHARED_ROOT).expanduser())
        rid = mgr._resolve_id(mgr.changes_dir, change_id)
        if mgr._load_core(mgr.changes_dir, rid) is None:
            console.print(f"\n  [red]Error:[/red] Change {change_id} not found\n")
            return

        was_scheduled = mgr._fold_record(mgr.changes_dir, rid, Change).status.value == "scheduled"
        mgr._append_event(mgr.changes_dir, rid, agent, "unschedule", note=note)
        chg = mgr._fold_record(mgr.changes_dir, rid, Change)
        if not was_scheduled:
            console.print(
                f"\n  [yellow]No change:[/yellow] {chg.id} was not scheduled "
                f"(status: {chg.status.value}).\n"
            )
            return
        console.print(f"\n  [green]Unscheduled:[/green] {chg.id} -> {chg.status.value}\n")

    # ── itil cab ──────────────────────────────────────────────────────

    @itil.group()
    def cab():
        """Change Advisory Board voting."""

    @cab.command("vote")
    @click.argument("change_id")
    @click.option(
        "--agent",
        default="human",
        help=(
            "Free-text voter label. Used as the voter identity only when the "
            "caller's authenticated identity cannot be resolved (CR change-mgmt P1.4)."
        ),
    )
    @click.option(
        "--decision",
        default="approved",
        type=click.Choice(["approved", "rejected", "abstain"]),
        help="Vote decision",
    )
    @click.option("--conditions", default="", help="Approval conditions")
    @click.option("--authorization", type=click.Path(path_type=Path), help="Signed human grant")
    @click.option("--target", default="", help="Exact governed mutation target")
    @click.option("--scope", default="", help="Exact governed mutation scope fingerprint")
    def cab_vote(change_id, agent, decision, conditions, authorization, target, scope):
        """Submit a CAB vote for a change.

        CR change-mgmt P1.4: the recorded voter is the caller's
        capauth-resolved authenticated identity when one is resolvable, never
        the free-text --agent label - closing the anonymous-voting hole where
        any caller could pass --agent human and unblock a change.
        """
        from ..itil import ITILManager

        mgr = ITILManager(Path(SHARED_ROOT).expanduser())
        subject = _resolve_cab_vote_subject()
        role = fingerprint = authorization_id = ""
        if authorization:
            if not target or not scope:
                raise click.ClickException(
                    "--target and --scope are required with --authorization"
                )
            envelope = _verified_cab_authorization(
                authorization, change_id, decision, target, scope
            )
            subject = envelope.issuer
            role = envelope.issuer_role
            fingerprint = envelope.issuer_fingerprint
            authorization_id = envelope.authorization_id
        elif subject is None and decision in {"approved", "rejected"}:
            raise click.ClickException(
                "a signed --authorization is required when no authenticated subject is available"
            )
        vote = mgr.submit_cab_vote(
            change_id=change_id,
            agent=agent,
            decision=decision,
            conditions=conditions,
            subject=subject,
            subject_role=role,
            subject_fingerprint=fingerprint,
            authorization_id=authorization_id,
        )
        console.print(
            f"\n  [green]Voted:[/green] {vote.agent} -> {vote.decision.value} "
            f"on {vote.change_id}\n"
        )

    @cab.command("authorize")
    @click.argument("change_id")
    @click.option("--decision", type=click.Choice(["approved", "rejected"]), required=True)
    @click.option("--target", required=True)
    @click.option("--scope", required=True)
    @click.option("--role", type=click.Choice(["owner", "approver"]), default="owner")
    @click.option("--ttl-minutes", type=click.IntRange(1, 60), default=10)
    @click.option("--output", type=click.Path(path_type=Path), required=True)
    def cab_authorize(change_id, decision, target, scope, role, ttl_minutes, output):
        """Create a short-lived, PGP-signed human CAB authorization."""
        from capauth.crypto import get_backend

        from ..operator_authorization import AuthorizationEnvelope, authorization_id

        home, profile = _human_profile()
        private_path = home / "identity" / "private.asc"
        if not private_path.is_file():
            raise click.ClickException(
                "human private key is not installed; restore it through the "
                "CapAuth custody ceremony"
            )
        now = datetime.now(timezone.utc)
        envelope = AuthorizationEnvelope(
            authorization_id="pending",
            issuer=(profile.entity.handle or profile.entity.name).split("@")[0].lower(),
            issuer_role=role,
            issuer_fingerprint=profile.key_info.fingerprint,
            action=f"itil.cab.vote.{decision}",
            target=target,
            change_id=change_id,
            scope=scope,
            issued_at=now.isoformat(),
            expires_at=(now + timedelta(minutes=ttl_minutes)).isoformat(),
            nonce=secrets.token_urlsafe(24),
        )
        envelope.authorization_id = authorization_id(envelope)
        passphrase = click.prompt("CapAuth key passphrase", hide_input=True, default="")
        envelope.signature = get_backend(profile.crypto_backend).sign(
            envelope.signing_bytes(), private_path.read_text(encoding="utf-8"), passphrase
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(envelope.model_dump(), indent=2) + "\n", encoding="utf-8")
        output.chmod(0o600)
        console.print(f"[green]Authorization written:[/green] {output}")

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
