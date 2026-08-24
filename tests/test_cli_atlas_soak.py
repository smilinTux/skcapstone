"""CLI tests for `skcapstone atlas soak record`/`report` (card 90b5b277).

These patch `skcapstone.operator_seat.soak.record`/`.report`/`.render`
directly (never a live fleet tree or subprocess) so they only exercise the
CLI's own wiring of flags onto the soak module.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from skcapstone.cli import main

_SAMPLE = {
    "schema": "skoperator.soak/v1",
    "at": "2026-08-24T00:00:00Z",
    "frozen": False,
    "apps": [],
}

_REPORT = {
    "schema": "skoperator.soak-report/v1",
    "at": "2026-08-24T00:00:00Z",
    "window_days": 21,
    "min_span_days": 7,
    "min_samples": 7,
    "sample_passes": 0,
    "apps": [],
}


def test_soak_record_prints_the_written_path():
    with patch(
        "skcapstone.operator_seat.soak.record",
        return_value={"sample": _SAMPLE, "path": Path("/tmp/x/node-a-2026-08-24.jsonl")},
    ) as mocked:
        result = CliRunner().invoke(main, ["atlas", "soak", "record"])
    assert result.exit_code == 0, result.output
    assert "node-a-2026-08-24.jsonl" in result.output
    mocked.assert_called_once()


def test_soak_record_json_emits_the_sample():
    with patch(
        "skcapstone.operator_seat.soak.record",
        return_value={"sample": _SAMPLE, "path": Path("/tmp/x/f.jsonl")},
    ):
        result = CliRunner().invoke(main, ["atlas", "soak", "record", "--json"])
    assert result.exit_code == 0, result.output
    assert "skoperator.soak/v1" in result.output


def test_soak_record_forwards_retention_days():
    with patch(
        "skcapstone.operator_seat.soak.record",
        return_value={"sample": _SAMPLE, "path": Path("/tmp/x/f.jsonl")},
    ) as mocked:
        CliRunner().invoke(main, ["atlas", "soak", "record", "--retention-days", "5"])
    _, kwargs = mocked.call_args
    assert kwargs["retention_days"] == 5


def test_soak_report_renders_by_default():
    with (
        patch("skcapstone.operator_seat.soak.report", return_value=_REPORT),
        patch("skcapstone.operator_seat.soak.render", return_value="rendered soak report"),
    ):
        result = CliRunner().invoke(main, ["atlas", "soak", "report"])
    assert result.exit_code == 0, result.output
    assert "rendered soak report" in result.output


def test_soak_report_json_emits_the_report():
    with patch("skcapstone.operator_seat.soak.report", return_value=_REPORT):
        result = CliRunner().invoke(main, ["atlas", "soak", "report", "--json"])
    assert result.exit_code == 0, result.output
    assert "skoperator.soak-report/v1" in result.output


def test_soak_report_forwards_gate_flags():
    with patch("skcapstone.operator_seat.soak.report", return_value=_REPORT) as mocked:
        CliRunner().invoke(
            main,
            [
                "atlas",
                "soak",
                "report",
                "--window-days",
                "14",
                "--min-span-days",
                "3",
                "--min-samples",
                "5",
            ],
        )
    _, kwargs = mocked.call_args
    assert kwargs == {"window_days": 14, "min_span_days": 3.0, "min_samples": 5}


def test_soak_report_defaults_pass_no_kwargs_when_flags_omitted():
    with patch("skcapstone.operator_seat.soak.report", return_value=_REPORT) as mocked:
        CliRunner().invoke(main, ["atlas", "soak", "report"])
    _, kwargs = mocked.call_args
    assert kwargs == {}
