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
    @click.option("--merged-commit", help="40-hex commit on the target branch; linked on success.")
    @click.option(
        "--repo-path",
        type=click.Path(path_type=Path),
        help="Local clone of the card's repository (required; no implicit git fetch).",
    )
    @click.option("--target-ref", default="origin/main", show_default=True)
    @click.option("--producer", help="Producer identity override when it cannot be determined.")
    @click.option("--allow-skips", is_flag=True, help="Approve a test log with skipped tests.")
    def coord_accept(
        source_card, agent, home, casey_authorization, casey_change_id,
        merged_commit, repo_path, target_ref, producer, allow_skips,
    ):  # fmt: skip
        """Complete a source card and its review.

        Requires: PASS from a non-producer reviewer, tests on the reviewed
        commit, a log with no skips (unless --allow-skips), and the reviewed
        code on --target-ref in --repo-path (merged_commit is an ancestor and
        either the reviewed head is too, or the squash patch-id matches).
        merged_commit comes from the card link or --merged-commit.
        """
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
            click.echo(
                accept_source(
                    home_path,
                    source_card,
                    agent,
                    merged_commit=merged_commit,
                    repo_path=repo_path,
                    target_ref=target_ref,
                    producer=producer,
                    allow_skips=allow_skips,
                )
            )
        except (AcceptRefusedError, OSError, RuntimeError, ValueError) as exc:
            click.echo(f"refused: {exc}", err=True)
            raise SystemExit(1) from None
