"""An explicit hash-fenced operator checkpoint for interrupted acceptance."""

import json
from pathlib import Path

import click

from ._common import AGENT_HOME


def register_acceptance_reseal(coord):
    """Register the single supported recovery boundary, with no unguarded mode."""
    from ..fleet.acceptance_reseal import GUARDS

    @coord.command("acceptance-reseal")
    @click.argument("review_card")
    @click.option("--review-claim", required=True)
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--agent", required=True)
    @click.option("--corrections", required=True, type=click.Path(exists=True, dir_okay=False))
    def command(review_card, review_claim, home, agent, corrections, **expected):
        """Reseal only an already-written step-08 PASS after exact metadata repairs."""
        from ..fleet.acceptance_reseal import reseal
        from ..fleet.production_review_finish import read_json
        from ..jarvis_emergency import authorize_coord_mutation
        from ..seat_boundaries import Action

        authorize_coord_mutation(agent, Action.LINK_CARD, review_card, None, None)
        try:
            result = reseal(
                Path(home).expanduser(),
                review_card,
                review_claim,
                agent,
                read_json(Path(corrections)),
                expected,
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise click.ClickException(str(exc)) from None
        click.echo(json.dumps(result, sort_keys=True))

    for name in GUARDS:
        click.option("--expected-" + name.replace("_", "-"), name, required=True)(command)
