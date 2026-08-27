"""Folded amendment commands: reprioritize, amend-criteria, void.

Same fold discipline as ``coord describe``: an appended, writer-attributed
event that the fold applies on read. Birth facts stay write-once in
``core.json``; every amendment is reversible by re-applying. ``void``
(card 325a737f) kills a mistaken card without completing it, so no Joules
are minted and the changelog stays clean.
"""

from __future__ import annotations

from pathlib import Path

import click

from ..coord_amendments import VALID_PRIORITIES
from ._common import AGENT_HOME, console


def register_coord_amend_commands(coord: click.Group) -> None:
    """Register the folded amendment verbs on the coord command group."""

    @coord.command("add-dependency")
    @click.argument("task_id")
    @click.option("--dependency", "dependency_id", required=True, help="Completed gate card ID.")
    @click.option("--reason", required=True, help="Governance reason retained in the audit event.")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--agent", default=None, help="Writer name (defaults to host).")
    def coord_add_dependency(task_id, dependency_id, reason, home, agent):
        """Append an idempotent dependency gate without rewriting card birth facts."""
        from ..coord_amendments import add_dependency
        from ._validators import validate_task_id

        validate_task_id(task_id)
        validate_task_id(dependency_id)
        try:
            changed = add_dependency(
                Path(home).expanduser(), task_id, dependency_id, agent or "", reason
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
        outcome = "Added" if changed else "Already present"
        console.print(f"\n  [green]{outcome} dependency {dependency_id} on {task_id}.[/]\n")

    @coord.command("remove-dependency")
    @click.argument("task_id")
    @click.option("--dependency", "dependency_id", required=True, help="Gate card ID to remove.")
    @click.option("--reason", required=True, help="Rollback reason retained in the audit event.")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--agent", default=None, help="Writer name (defaults to host).")
    def coord_remove_dependency(task_id, dependency_id, reason, home, agent):
        """Append a reversible dependency rollback without rewriting card birth facts."""
        from ..coord_amendments import remove_dependency
        from ._validators import validate_task_id

        validate_task_id(task_id)
        validate_task_id(dependency_id)
        try:
            changed = remove_dependency(
                Path(home).expanduser(), task_id, dependency_id, agent or "", reason
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
        outcome = "Removed" if changed else "Already absent"
        console.print(f"\n  [green]{outcome} dependency {dependency_id} on {task_id}.[/]\n")

    @coord.command("reprioritize")
    @click.argument("task_id")
    @click.option(
        "--priority",
        required=True,
        type=click.Choice(list(VALID_PRIORITIES)),
        help="New priority for the card.",
    )
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--agent", default=None, help="Writer name (defaults to host).")
    def coord_reprioritize(task_id, priority, home, agent):
        """Amend a card's priority (folded, never rewrites core.json).

        The birth priority stays visible in core.json; the amendment is one
        appended event, attributed to its writer and reversed by
        reprioritizing again.
        """
        from ..coord_amendments import reprioritize

        home_path = Path(home).expanduser()
        reprioritize(home_path, task_id, priority, agent or "")
        console.print(f"\n  [green]Reprioritized {task_id} to {priority.upper()}.[/]\n")

    @coord.command("amend-criteria")
    @click.argument("task_id")
    @click.option(
        "--criteria",
        multiple=True,
        help="Acceptance criterion (repeatable). Replaces the folded list.",
    )
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--agent", default=None, help="Writer name (defaults to host).")
    def coord_amend_criteria(task_id, criteria, home, agent):
        """Replace a card's acceptance criteria (folded, never rewrites core.json).

        The original list stays visible in core.json; the amendment is one
        appended event carrying the full replacement list (latest event
        wins), attributed to its writer and reversed by amending again.
        """
        from ..coord_amendments import amend_criteria, current_acceptance_criteria

        if not criteria:
            raise click.UsageError("Pass at least one --criteria.")

        home_path = Path(home).expanduser()
        try:
            amend_criteria(home_path, task_id, list(criteria), agent or "")
            folded = current_acceptance_criteria(home_path, task_id)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
        console.print(f"\n  [green]Amended criteria on {task_id} ({len(folded)} criterion/a).[/]")
        for c in folded:
            console.print(f"    [dim]- {c}[/]")
        console.print()

    @coord.command("void")
    @click.argument("task_id")
    @click.option("--reason", required=True, help="Why the card is being voided (audit).")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--agent", default=None, help="Writer name (defaults to host).")
    @click.option(
        "--force-terminal",
        is_flag=True,
        default=False,
        help="Record an audit-only void on an already-completed card. The fold "
        "will STILL report the card done.",
    )
    def coord_void(task_id, reason, home, agent, force_terminal):
        """Void a mistakenly created card WITHOUT completing it.

        Appends a writer-attributed void event and archives the card: it
        leaves the active board, mints no Joules (completion is the only
        minting path), and never appears in 'coord changelog' output. The
        card stays on disk and remains foldable for audit.
        """
        from ..coord_amendments import void_card

        home_path = Path(home).expanduser()
        try:
            void_card(home_path, task_id, reason, agent or "", force_terminal=force_terminal)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
        if force_terminal:
            console.print(
                f"\n  [yellow]Recorded an audit-only void on {task_id}.[/] "
                f"[dim]{reason}[/]\n"
                "  [yellow]The card is complete and terminal states are sticky, so "
                "it STILL folds to done.[/]\n"
                "  [dim]Nothing downstream will see this card as withdrawn.[/]\n"
            )
        else:
            console.print(f"\n  [green]Voided {task_id}.[/] [dim]{reason}[/]\n")
