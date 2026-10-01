"""Exact controller reconciliation for a historical PR association."""

import json
from pathlib import Path

import click

from ._common import AGENT_HOME


def register_historical_pr_command(coord: click.Group) -> None:
    """Register the narrow guarded repair without an unguarded mode."""

    @coord.command("archive-historical-pr")
    @click.argument("card_id")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--agent", required=True)
    @click.option("--expected-card-revision", required=True)
    @click.option("--expected-history-revision", required=True)
    @click.option("--expected-claim-revision", required=True)
    @click.option("--expected-pr", required=True)
    @click.option("--expected-repository", required=True)
    @click.option("--transition-id", required=True)
    def archive(card_id: str, home: str, agent: str, **guards: str) -> None:
        """Archive an exact mismatched PR, retaining history and current outcome."""
        from ..historical_pr import archive_historical_pr
        from ..jarvis_emergency import authorize_coord_mutation
        from ..seat_boundaries import Action

        authorize_coord_mutation(agent, Action.LINK_CARD, card_id, None, None)
        try:
            result = archive_historical_pr(Path(home).expanduser(), card_id, agent, **guards)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
        click.echo(json.dumps(result, sort_keys=True))
