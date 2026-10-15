"""``skcapstone coord accept``: thin CLI over skcapstone.simple_accept."""

from __future__ import annotations

from pathlib import Path

import click

from ._common import AGENT_HOME
from ._validators import validate_agent_name, validate_task_id


def register_coord_accept_command(coord: click.Group) -> None:
    @coord.command("accept")
    @click.argument("source_card")
    @click.option("--agent", required=True, help="Operator identity (lumina-nor or jarvis).")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--casey-authorization", type=click.Path(path_type=Path))
    @click.option("--casey-change-id")
    def coord_accept(source_card, agent, home, casey_authorization, casey_change_id):
        """Complete a source card and its review when PASS + tests-on-same-commit hold."""
        from ..jarvis_emergency import authorize_coord_mutation
        from ..seat_boundaries import Action
        from ..simple_accept import AcceptRefusedError, accept_source

        validate_task_id(source_card)
        validate_agent_name(agent)
        home_path = Path(home).expanduser()
        authorize_coord_mutation(
            agent, Action.COMPLETE_CARD, source_card, casey_authorization, casey_change_id,
            home=home_path,
        )  # fmt: skip
        try:
            click.echo(accept_source(home_path, source_card, agent))
        except (AcceptRefusedError, OSError, RuntimeError, ValueError) as exc:
            click.echo(f"refused: {exc}", err=True)
            raise SystemExit(1) from None
