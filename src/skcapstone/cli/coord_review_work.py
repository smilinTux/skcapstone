"""Mediated connection to the existing independent review-work helper."""

import json
from pathlib import Path

import click

from ._common import AGENT_HOME
from ._validators import validate_agent_name, validate_task_id


def register_coord_review_work(coord: click.Group) -> None:
    """Register one exact-source opener, not a launcher or completion shortcut."""

    @coord.command("review-work")
    @click.argument("task_id")
    @click.option(
        "--producer", required=True, help="Exact held or cleanly released source-claim producer."
    )
    @click.option("--expected-source-revision", required=True)
    @click.option("--expected-claim-revision", required=True)
    @click.option(
        "--agent",
        default="link",
        type=click.Choice(["link"]),
        help="Existing review-assignment seat; no worker identity impersonation.",
    )
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def coord_review_work(
        task_id, producer, expected_source_revision, expected_claim_revision, agent, home
    ):
        """Open one review of the current typed provisional source candidate.

        Outputs JSON. Does not claim, launch, complete, publish or merge work.
        """
        from ..guarded_review_work import open_guarded_review
        from ..jarvis_emergency import authorize_coord_mutation
        from ..seat_boundaries import Action

        validate_task_id(task_id)
        validate_agent_name(producer)
        validate_agent_name(agent)
        authorize_coord_mutation(agent, Action.CREATE_CARD, task_id, None, None)
        try:
            result = open_guarded_review(
                Path(home).expanduser(),
                task_id,
                producer,
                expected_source_revision=expected_source_revision,
                expected_claim_revision=expected_claim_revision,
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
        click.echo(json.dumps(result.as_dict(), sort_keys=True))
