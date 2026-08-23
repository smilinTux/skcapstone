"""CLI tests for `skcapstone atlas eyes --strict` (card 504d0046).

`--strict` turns "zero CONFLICT rows" from a report a human has to read
carefully into a script-checkable exit code: a lying lane must not be
promoted to source of truth by going unnoticed in a CI log. These tests patch
``skcapstone.operator_seat.eyes.assess`` directly (never touch a live fleet or
network) so they only exercise the CLI's own wiring of ``--strict`` onto
``assert_no_conflicts``.
"""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from skcapstone.cli import main

_CLEAN = {
    "schema": "skoperator.eyes/v1",
    "at": "2026-08-23T00:00:00Z",
    "frozen": False,
    "freeze_reason": "",
    "apps": [{"name": "appx", "cli": "appx operator", "conflicts": []}],
    "itil": {"available": False, "detail": "n/a"},
    "unregistered_modules": [],
    "blind_spots": [],
}

_CONFLICTED = {
    **_CLEAN,
    "apps": [
        {
            "name": "skcomms",
            "cli": "skcomms operator",
            "conflicts": [{"type": "PathHealthy", "cli": "True", "seat": "False"}],
        }
    ],
}


def _render(_assessment: dict) -> str:
    return "rendered report"


def test_strict_exits_zero_when_no_conflicts():
    with (
        patch("skcapstone.operator_seat.eyes.assess", return_value=_CLEAN),
        patch("skcapstone.operator_seat.eyes.render", side_effect=_render),
    ):
        result = CliRunner().invoke(main, ["atlas", "eyes", "--strict"])
    assert result.exit_code == 0, result.output
    assert "rendered report" in result.output


def test_strict_exits_nonzero_and_names_the_conflict():
    with (
        patch("skcapstone.operator_seat.eyes.assess", return_value=_CONFLICTED),
        patch("skcapstone.operator_seat.eyes.render", side_effect=_render),
    ):
        result = CliRunner().invoke(main, ["atlas", "eyes", "--strict"])
    assert result.exit_code != 0
    # The report still printed: --strict fails AFTER reporting, never instead of it.
    assert "rendered report" in result.output
    assert "skcomms.PathHealthy" in result.output


def test_without_strict_conflicts_do_not_fail_the_command():
    with (
        patch("skcapstone.operator_seat.eyes.assess", return_value=_CONFLICTED),
        patch("skcapstone.operator_seat.eyes.render", side_effect=_render),
    ):
        result = CliRunner().invoke(main, ["atlas", "eyes"])
    assert result.exit_code == 0, result.output
