"""`skcapstone atlas` - read-only views of the ATLAS operator estate.

`eyes` is the freeze-proof estate assessor. `soak` is the Phase 3 dual-read
instrument built on top of it (card 90b5b277, docs/OPERATOR_PLANE_MIGRATION.md):
`soak record` appends one dual-lane sample per pass, `soak report` answers the
two per-app gate questions (LaneConflict count, Unknown-regression count) over
a window and says which apps are ready to demote their old lane. Anything
under this group must stay read-only; actuation belongs to `skoperator`,
behind the freeze, where it can be stopped.
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

    @atlas.group("soak")
    def atlas_soak():
        """Phase 3 dual-read soak (card 90b5b277): record + gate report.

        Read-only and freeze-independent, same as `atlas eyes` -- it never
        registers an endpoint and never actuates.
        """

    @atlas_soak.command("record")
    @click.option("--json", "as_json", is_flag=True, help="Emit the recorded sample as JSON.")
    @click.option(
        "--retention-days",
        type=int,
        default=None,
        help="Delete sample files older than this many days (default: 21).",
    )
    def atlas_soak_record(as_json: bool, retention_days: int | None):
        """One dual-read pass: assess via `eyes`, append a sample, prune old files."""
        from skcapstone.fleet.paths import default_paths
        from skcapstone.operator_seat import soak

        kwargs = {}
        if retention_days is not None:
            kwargs["retention_days"] = retention_days
        result = soak.record(default_paths(), **kwargs)
        if as_json:
            click.echo(_json.dumps(result["sample"], indent=2))
        else:
            click.echo(f"recorded soak sample -> {result['path']}")

    @atlas_soak.command("report")
    @click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
    @click.option(
        "--window-days",
        type=int,
        default=None,
        help="How many days of recorded samples to consider (default: 21).",
    )
    @click.option(
        "--min-span-days",
        type=float,
        default=None,
        help="Minimum sample time-span, in days, to call an app READY (default: 7).",
    )
    @click.option(
        "--min-samples",
        type=int,
        default=None,
        help="Minimum comparable sample count to call an app READY (default: 7).",
    )
    def atlas_soak_report(
        as_json: bool,
        window_days: int | None,
        min_span_days: float | None,
        min_samples: int | None,
    ):
        """Answer the two Phase 3 gate questions per app: LaneConflict count
        and Unknown-regression count, plus a READY/BLOCKED/SOAKING/PENDING/
        NO-ENDPOINT verdict for each.
        """
        from skcapstone.fleet.paths import default_paths
        from skcapstone.operator_seat import soak

        kwargs = {}
        if window_days is not None:
            kwargs["window_days"] = window_days
        if min_span_days is not None:
            kwargs["min_span_days"] = min_span_days
        if min_samples is not None:
            kwargs["min_samples"] = min_samples
        rep = soak.report(default_paths(), **kwargs)
        if as_json:
            click.echo(_json.dumps(rep, indent=2))
        else:
            click.echo(soak.render(rep))
