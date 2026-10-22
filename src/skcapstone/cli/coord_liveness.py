"""Native lifecycle writer entrypoint for exact worker liveness."""

from pathlib import Path

import click

from ._common import AGENT_HOME


def register_coord_liveness_command(coord: click.Group) -> None:
    """Register the narrowly scoped observer command, not a generic owner bypass."""

    @coord.command("worker-liveness")
    @click.argument("task_id")
    @click.option("--owner", required=True)
    @click.option("--expected-claim-revision", "claim", required=True)
    @click.option("--state", type=click.Choice(["active", "terminal"]), required=True)
    @click.option("--unit", required=True)
    @click.option("--pid", type=click.IntRange(min=1), required=True)
    @click.option("--invocation", required=True)
    @click.option("--agent", "actor", required=True)
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def worker_liveness(task_id, home, **kwargs):
        """Publish only if the exact worker process and claim remain current."""
        from ..fleet.liveness_publication import publish_liveness

        try:
            changed = publish_liveness(Path(home).expanduser(), card=task_id, **kwargs)
        except (ValueError, OSError) as exc:
            raise click.ClickException(str(exc)) from None
        click.echo("Published worker_liveness" if changed else "No liveness change")
