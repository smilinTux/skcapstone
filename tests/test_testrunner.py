"""Tests for skcapstone.testrunner module.

Covers PackageResult, TestReport, _tail, _parse_pytest_summary,
and run_all_tests with mocked subprocess calls.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from skcapstone.testrunner import (
    ECOSYSTEM_PACKAGES,
    PackageResult,
    TestReport,
    _parse_pytest_summary,
    _tail,
    run_all_tests,
)


# ── TestPackageResult ────────────────────────────────────────────


class TestPackageResult:
    """Tests for the PackageResult dataclass."""

    def test_default_values(self):
        """Default numeric fields are zero, available is True."""
        r = PackageResult(name="pkg")
        assert r.passed == 0
        assert r.failed == 0
        assert r.errors == 0
        assert r.skipped == 0
        assert r.duration_s == 0.0
        assert r.exit_code == -1
        assert r.output == ""
        assert r.available is True

    def test_total_property(self):
        """total = passed + failed + errors."""
        r = PackageResult(name="pkg", passed=3, failed=2, errors=1)
        assert r.total == 6

    def test_success_when_all_pass(self):
        """success is True when exit_code=0, failed=0, errors=0."""
        r = PackageResult(name="pkg", passed=5, exit_code=0)
        assert r.success is True

    def test_success_false_when_failed_gt_zero(self):
        """success is False when failed > 0."""
        r = PackageResult(name="pkg", passed=4, failed=1, exit_code=1)
        assert r.success is False

    def test_success_false_when_errors_gt_zero(self):
        """success is False when errors > 0 even if exit_code=0."""
        r = PackageResult(name="pkg", passed=4, errors=1, exit_code=0)
        assert r.success is False

    def test_to_dict_serialization(self):
        """to_dict returns expected keys and computed values."""
        r = PackageResult(
            name="demo",
            passed=10,
            failed=2,
            errors=1,
            skipped=3,
            duration_s=1.456,
            exit_code=1,
            available=True,
        )
        d = r.to_dict()
        assert d["name"] == "demo"
        assert d["passed"] == 10
        assert d["failed"] == 2
        assert d["errors"] == 1
        assert d["skipped"] == 3
        assert d["total"] == 13  # 10 + 2 + 1
        assert d["duration_s"] == 1.46  # rounded to 2 decimals
        assert d["success"] is False
        assert d["available"] is True


# ── TestTestReport ───────────────────────────────────────────────


class TestTestReport:
    """Tests for the TestReport dataclass."""

    def test_default_values(self):
        """Defaults to empty results and 0 duration."""
        report = TestReport()
        assert report.results == []
        assert report.duration_s == 0.0

    def test_total_passed_aggregation(self):
        """total_passed sums passed across all packages."""
        report = TestReport(results=[
            PackageResult(name="a", passed=3, exit_code=0),
            PackageResult(name="b", passed=7, exit_code=0),
        ])
        assert report.total_passed == 10

    def test_total_failed_aggregation(self):
        """total_failed sums failed across all packages."""
        report = TestReport(results=[
            PackageResult(name="a", failed=1, exit_code=1),
            PackageResult(name="b", failed=4, exit_code=1),
        ])
        assert report.total_failed == 5

    def test_all_passed_when_all_succeed(self):
        """all_passed is True when every available package succeeds."""
        report = TestReport(results=[
            PackageResult(name="a", passed=5, exit_code=0),
            PackageResult(name="b", passed=3, exit_code=0),
        ])
        assert report.all_passed is True

    def test_all_passed_false_when_one_fails(self):
        """all_passed is False when at least one package fails."""
        report = TestReport(results=[
            PackageResult(name="a", passed=5, exit_code=0),
            PackageResult(name="b", passed=2, failed=1, exit_code=1),
        ])
        assert report.all_passed is False

    def test_packages_tested_counts_only_available(self):
        """packages_tested counts only packages with available=True."""
        report = TestReport(results=[
            PackageResult(name="a", passed=5, exit_code=0, available=True),
            PackageResult(name="b", available=False),
            PackageResult(name="c", passed=2, exit_code=0, available=True),
        ])
        assert report.packages_tested == 2

    def test_to_dict_serialization(self):
        """to_dict returns all top-level keys and nested package dicts."""
        report = TestReport(
            results=[
                PackageResult(name="x", passed=4, exit_code=0),
            ],
            duration_s=2.789,
        )
        d = report.to_dict()
        assert d["all_passed"] is True
        assert d["total_passed"] == 4
        assert d["total_failed"] == 0
        assert d["total_errors"] == 0
        assert d["packages_tested"] == 1
        assert d["duration_s"] == 2.79
        assert len(d["packages"]) == 1
        assert d["packages"][0]["name"] == "x"


# ── TestTail ─────────────────────────────────────────────────────


class TestTail:
    """Tests for the _tail helper."""

    def test_basic_tail(self):
        """Returns the last N lines."""
        text = "line1\nline2\nline3\nline4\nline5"
        result = _tail(text, 3)
        assert result == "line3\nline4\nline5"

    def test_tail_more_than_available(self):
        """Returns all lines when N exceeds total."""
        text = "a\nb"
        result = _tail(text, 10)
        assert result == "a\nb"

    def test_empty_string(self):
        """Handles empty string without error."""
        result = _tail("", 5)
        assert result == ""


# ── TestParsePytestSummary ───────────────────────────────────────


class TestParsePytestSummary:
    """Tests for _parse_pytest_summary."""

    def test_parses_passed_only(self):
        """Extracts passed count from a simple summary line."""
        output = "===== 5 passed in 0.42s ====="
        r = PackageResult(name="t")
        _parse_pytest_summary(output, r)
        assert r.passed == 5
        assert r.failed == 0

    def test_parses_passed_and_failed(self):
        """Extracts both passed and failed counts."""
        output = "===== 3 passed, 1 failed in 1.2s ====="
        r = PackageResult(name="t")
        _parse_pytest_summary(output, r)
        assert r.passed == 3
        assert r.failed == 1

    def test_parses_passed_and_error(self):
        """Extracts passed and error counts."""
        output = "===== 2 passed, 1 error in 0.8s ====="
        r = PackageResult(name="t")
        _parse_pytest_summary(output, r)
        assert r.passed == 2
        assert r.errors == 1

    def test_parses_passed_and_skipped(self):
        """Extracts passed and skipped counts."""
        output = "===== 5 passed, 2 skipped in 0.5s ====="
        r = PackageResult(name="t")
        _parse_pytest_summary(output, r)
        assert r.passed == 5
        assert r.skipped == 2


# ── TestRunAllTests ──────────────────────────────────────────────


class TestRunAllTests:
    """Tests for run_all_tests."""

    def test_missing_test_dirs_marked_unavailable(self, tmp_path: Path):
        """Packages whose test dirs don't exist are marked unavailable."""
        report = run_all_tests(tmp_path, packages=["skcapstone"])
        pkg = report.results[0]
        assert pkg.available is False
        assert "not found" in pkg.output

    def test_filter_by_package_names(self, tmp_path: Path):
        """Only requested packages appear in the report."""
        report = run_all_tests(tmp_path, packages=["skcomm", "skchat"])
        names = [r.name for r in report.results]
        assert names == ["skcomm", "skchat"]
