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
    @click.option("--producer", required=True, help="Current exact source-claim owner.")
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

    @coord.command("review-replace")
    @click.argument("task_id")
    @click.option("--authorization", required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option("--agent", required=True, type=click.Choice(["jarvis"]))
    @click.option(
        "--check",
        is_flag=True,
        help="Validate exact custody without appending authorization or opening work.",
    )
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def coord_review_replace(task_id, authorization, agent, check, home):
        """Authorize one replacement, preserving invalid evidence and all claims.

        The exact operator document is validated against native source/history,
        retained evidence and current process death. No worker is launched.
        """
        from ..review_replacement import authorize_replacement

        validate_task_id(task_id)
        path = Path(authorization)
        try:
            import os
            import stat

            if path.resolve(strict=True) != path.absolute():
                raise ValueError("replacement authorization symlink invalid")
            with os.fdopen(
                os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb"
            ) as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                    raise ValueError("replacement authorization must be private regular file")
                data = stream.read(65537)
            if len(data) > 65536:
                raise ValueError("replacement authorization too large")
            request = json.loads(data)
            if not isinstance(request, dict) or request.get("source_card") != task_id:
                raise ValueError("replacement authorization source mismatch")
            result = authorize_replacement(
                Path(home).expanduser(), request, actor=agent, check_only=check
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise click.ClickException(str(exc)) from None
        click.echo(json.dumps(result, sort_keys=True))
