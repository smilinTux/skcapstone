"""Tests for the skcapstone test CLI command (test_cmd.py).

Covers:
  - Help output is rendered correctly
  - Invalid package names are rejected with a clear error
  - JSON output mode produces valid structured JSON
  - Per-package table renders for available/unavailable packages
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from skcapstone.cli import main
from skcapstone.testrunner import PackageResult, TestReport


@pytest.fixture()
def runner():
    return CliRunner(mix_stderr=False)


@pytest.fixture()
def mock_report_all_pass():
    """A TestReport where every package passes."""
    return TestReport(
        results=[
            PackageResult(name="skcapstone", passed=10, exit_code=0, duration_s=1.2),
            PackageResult(name="skcomm", passed=5, exit_code=0, duration_s=0.8),
        ],
        duration_s=2.0,
    )


@pytest.fixture()
def mock_report_with_failure():
    """A TestReport with one failing package."""
    return TestReport(
        results=[
            PackageResult(name="skcapstone", passed=10, exit_code=0, duration_s=1.2),
            PackageResult(
                name="skcomm",
                passed=3,
                failed=2,
                exit_code=1,
                duration_s=0.9,
                output="FAILED tests/test_foo.py::test_bar",
            ),
        ],
        duration_s=2.1,
    )


@pytest.fixture()
def mock_report_unavailable():
    """A TestReport with one unavailable package."""
    return TestReport(
        results=[
            PackageResult(
                name="capauth",
                available=False,
                output="Test directory not found",
            ),
        ],
        duration_s=0.0,
    )


# ── Help output ──────────────────────────────────────────────────


class TestTestCmdHelp:
    """Ensure help text is registered and contains key information."""

    def test_help_renders(self, runner):
        """--help exits 0 and shows package names."""
        result = runner.invoke(main, ["test", "--help"])
        assert result.exit_code == 0
        assert "skcapstone" in result.output
        assert "--package" in result.output

    def test_help_lists_options(self, runner):
        """--help shows all major options."""
        result = runner.invoke(main, ["test", "--help"])
        assert "--fast" in result.output
        assert "--verbose" in result.output
        assert "--json-out" in result.output
        assert "--timeout" in result.output


# ── Invalid package validation ───────────────────────────────────


class TestInvalidPackage:
    """Unknown package names should be rejected immediately."""

    def test_invalid_package_exits_nonzero(self, runner):
        """Passing an unknown --package name exits with code 1."""
        result = runner.invoke(main, ["test", "--package", "does-not-exist"])
        assert result.exit_code == 1

    def test_invalid_package_shows_valid_names(self, runner):
        """Error message lists the valid package names."""
        result = runner.invoke(main, ["test", "--package", "bogus"])
        assert "bogus" in result.output
        assert "skcapstone" in result.output

    def test_valid_package_accepted(self, runner, mock_report_all_pass):
        """A valid package name is accepted and run_all_tests is called."""
        with patch(
            "skcapstone.cli.test_cmd.run_all_tests",
            return_value=mock_report_all_pass,
        ):
            result = runner.invoke(main, ["test", "--package", "skcomm"])
        assert result.exit_code == 0


# ── JSON output mode ─────────────────────────────────────────────


class TestJsonOutput:
    """--json-out should produce parseable JSON with the right structure."""

    def test_json_out_is_parseable(self, runner, mock_report_all_pass):
        """JSON output can be loaded by json.loads."""
        with patch(
            "skcapstone.cli.test_cmd.run_all_tests",
            return_value=mock_report_all_pass,
        ):
            result = runner.invoke(main, ["test", "--json-out"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "all_passed" in data
        assert "packages" in data

    def test_json_exit_nonzero_on_failure(self, runner, mock_report_with_failure):
        """JSON mode still exits 1 when tests fail."""
        with patch(
            "skcapstone.cli.test_cmd.run_all_tests",
            return_value=mock_report_with_failure,
        ):
            result = runner.invoke(main, ["test", "--json-out"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["all_passed"] is False

    def test_json_has_package_details(self, runner, mock_report_all_pass):
        """Each package result appears in the packages array."""
        with patch(
            "skcapstone.cli.test_cmd.run_all_tests",
            return_value=mock_report_all_pass,
        ):
            result = runner.invoke(main, ["test", "--json-out"])
        data = json.loads(result.output)
        pkg_names = [p["name"] for p in data["packages"]]
        assert "skcapstone" in pkg_names


# ── Table rendering ──────────────────────────────────────────────


class TestTableRendering:
    """Rich table should contain pass/fail/skip columns."""

    def test_pass_table_shows_pass(self, runner, mock_report_all_pass):
        """PASS appears in the output when all tests succeed."""
        with patch(
            "skcapstone.cli.test_cmd.run_all_tests",
            return_value=mock_report_all_pass,
        ):
            result = runner.invoke(main, ["test"])
        assert result.exit_code == 0
        assert "PASS" in result.output

    def test_fail_table_shows_fail(self, runner, mock_report_with_failure):
        """FAIL appears in the output when a package fails."""
        with patch(
            "skcapstone.cli.test_cmd.run_all_tests",
            return_value=mock_report_with_failure,
        ):
            result = runner.invoke(main, ["test"])
        assert result.exit_code == 1
        assert "FAIL" in result.output

    def test_unavailable_package_shows_na(self, runner, mock_report_unavailable):
        """Packages without test dirs show N/A, not an error."""
        with patch(
            "skcapstone.cli.test_cmd.run_all_tests",
            return_value=mock_report_unavailable,
        ):
            result = runner.invoke(main, ["test"])
        assert "N/A" in result.output

    def test_show_output_flag_prints_failures(self, runner, mock_report_with_failure):
        """--show-output flag prints pytest output for failing packages."""
        with patch(
            "skcapstone.cli.test_cmd.run_all_tests",
            return_value=mock_report_with_failure,
        ):
            result = runner.invoke(main, ["test", "--show-output"])
        assert "test_bar" in result.output
