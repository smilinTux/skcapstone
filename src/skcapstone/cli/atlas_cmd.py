"""`skcapstone atlas` - read-only views of the ATLAS operator estate.

Only `eyes` lives here today: the freeze-proof estate assessor. Anything under
this group must stay read-only; actuation belongs to `skoperator`, behind the
freeze, where it can be stopped.
"""

from __future__ import annotations

import json as _json

import click


def register_atlas_commands(main: click.Group) -> None:
    """Register the atlas command group."""

    @main.group()
    def atlas():
        """ATLAS operator estate - read-only views (never actuates)."""

    @atlas.command("eyes")
    @click.option(
        "--json", "as_json", is_flag=True, help="Emit the skoperator.eyes/v1 assessment as JSON."
    )
    @click.option(
        "--timeout",
        type=float,
        default=None,
        help="Per-app deadline in seconds for the out-of-process cli lane (default 15).",
    )
    @click.option(
        "--strict",
        is_flag=True,
        help=(
            "Exit non-zero (after printing the report) if any app has a lane "
            "conflict. Per PR #179's P0 gate: 'eyes CONFLICT=0' becomes a "
            "script-checkable exit code instead of a report a human has to "
            "read carefully; a lying lane must not be promoted to source of "
            "truth by going unnoticed in a CI log."
        ),
    )
    def atlas_eyes(as_json: bool, timeout: float | None, strict: bool):
        """Assess the whole operator estate in one read-only pass.

        Works WHILE FROZEN: `observe` is read-only and freeze-independent.
        Never calls `act`, never touches the freeze, writes nothing anywhere.
        """
        from skcapstone.fleet.paths import default_paths
        from skcapstone.operator_seat import eyes

        kwargs = {}
        if timeout is not None:
            kwargs["cli_timeout"] = timeout
        assessment = eyes.assess(default_paths(), **kwargs)
        if as_json:
            click.echo(_json.dumps(assessment, indent=2))
        else:
            click.echo(eyes.render(assessment))
        if strict:
            try:
                eyes.assert_no_conflicts(assessment)
            except eyes.LaneConflictError as exc:
                raise click.ClickException(str(exc)) from exc
