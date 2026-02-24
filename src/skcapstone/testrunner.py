"""
Unified test runner for the sovereign agent ecosystem.

Discovers all packages in the monorepo, runs pytest for each,
and presents a consolidated pass/fail summary. Works from any
terminal — no CI server, no IDE, no special tooling.

Usage:
    skcapstone test                     # run all packages
    skcapstone test --package skcomm    # run one package
    skcapstone test --fast              # stop on first failure
    skcapstone test --json-out          # machine-readable results
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


ECOSYSTEM_PACKAGES = [
    {"name": "skcapstone", "path": "skcapstone", "tests": "skcapstone/tests"},
    {"name": "capauth", "path": "capauth", "tests": "capauth/tests"},
    {"name": "skcomm", "path": "skcomm", "tests": "skcomm/tests"},
    {"name": "skchat", "path": "skchat", "tests": "skchat/tests"},
    {"name": "skmemory", "path": "skmemory", "tests": "skmemory/tests"},
    {"name": "cloud9-python", "path": "cloud9-python", "tests": "cloud9-python/tests"},
]


@dataclass
class PackageResult:
    """Test results for a single package.

    Attributes:
        name: Package name.
        passed: Number of tests passed.
        failed: Number of tests failed.
        errors: Number of collection/import errors.
        skipped: Number of skipped tests.
        duration_s: Total runtime in seconds.
        exit_code: Pytest exit code.
        output: Stdout from pytest (last N lines).
        available: Whether the package tests were found.
    """

    name: str
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    duration_s: float = 0.0
    exit_code: int = -1
    output: str = ""
    available: bool = True

    @property
    def total(self) -> int:
        """Total tests executed."""
        return self.passed + self.failed + self.errors

    @property
    def success(self) -> bool:
        """Whether all tests passed."""
        return self.exit_code == 0 and self.failed == 0 and self.errors == 0

    def to_dict(self) -> dict:
        """Serialize to dict.

        Returns:
            dict: Package test results.
        """
        return {
            "name": self.name,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "skipped": self.skipped,
            "total": self.total,
            "duration_s": round(self.duration_s, 2),
            "success": self.success,
            "available": self.available,
        }


@dataclass
class TestReport:
    """Consolidated test results across all packages.

    Attributes:
        results: Per-package results.
        duration_s: Total runtime.
    """

    results: list[PackageResult] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def total_passed(self) -> int:
        """Total passing tests across all packages."""
        return sum(r.passed for r in self.results)

    @property
    def total_failed(self) -> int:
        """Total failing tests across all packages."""
        return sum(r.failed for r in self.results)

    @property
    def total_errors(self) -> int:
        """Total errors across all packages."""
        return sum(r.errors for r in self.results)

    @property
    def all_passed(self) -> bool:
        """Whether every package passed."""
        return all(r.success for r in self.results if r.available)

    @property
    def packages_tested(self) -> int:
        """Number of packages actually tested."""
        return sum(1 for r in self.results if r.available)

    def to_dict(self) -> dict:
        """Serialize the full report.

        Returns:
            dict: Complete test report.
        """
        return {
            "all_passed": self.all_passed,
            "total_passed": self.total_passed,
            "total_failed": self.total_failed,
            "total_errors": self.total_errors,
            "packages_tested": self.packages_tested,
            "duration_s": round(self.duration_s, 2),
            "packages": [r.to_dict() for r in self.results],
        }


def run_all_tests(
    monorepo_root: Path,
    packages: Optional[list[str]] = None,
    fail_fast: bool = False,
    verbose: bool = False,
    timeout: int = 120,
) -> TestReport:
    """Run pytest across ecosystem packages.

    Args:
        monorepo_root: Root of the monorepo (where package dirs live).
        packages: Restrict to these package names. None = all.
        fail_fast: Stop after first package failure.
        verbose: Pass -v to pytest.
        timeout: Per-package timeout in seconds.

    Returns:
        TestReport: Consolidated results.
    """
    report = TestReport()
    start = time.monotonic()

    targets = ECOSYSTEM_PACKAGES
    if packages:
        pkg_set = set(packages)
        targets = [p for p in targets if p["name"] in pkg_set]

    for pkg_info in targets:
        test_dir = monorepo_root / pkg_info["tests"]
        if not test_dir.exists():
            report.results.append(PackageResult(
                name=pkg_info["name"],
                available=False,
                output=f"Test directory not found: {test_dir}",
            ))
            continue

        result = _run_package_tests(
            monorepo_root, pkg_info, verbose=verbose, timeout=timeout,
        )
        report.results.append(result)

        if fail_fast and not result.success:
            break

    report.duration_s = time.monotonic() - start
    return report


def _run_package_tests(
    root: Path,
    pkg_info: dict,
    verbose: bool = False,
    timeout: int = 120,
) -> PackageResult:
    """Run pytest for a single package.

    Args:
        root: Monorepo root.
        pkg_info: Package metadata dict.
        verbose: Pass -v to pytest.
        timeout: Timeout in seconds.

    Returns:
        PackageResult: Test results for this package.
    """
    result = PackageResult(name=pkg_info["name"])
    test_dir = root / pkg_info["tests"]

    cmd = [
        sys.executable, "-m", "pytest",
        str(test_dir),
        "--tb=line",
        "-q",
        "--no-header",
    ]
    if verbose:
        cmd.append("-v")

    start = time.monotonic()

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(root),
        )
        result.exit_code = proc.returncode
        result.duration_s = time.monotonic() - start

        output = proc.stdout + proc.stderr
        result.output = _tail(output, 30)

        _parse_pytest_summary(output, result)

    except subprocess.TimeoutExpired:
        result.duration_s = time.monotonic() - start
        result.exit_code = -1
        result.errors = 1
        result.output = f"TIMEOUT after {timeout}s"

    except Exception as exc:
        result.duration_s = time.monotonic() - start
        result.exit_code = -1
        result.errors = 1
        result.output = str(exc)

    return result


def _parse_pytest_summary(output: str, result: PackageResult) -> None:
    """Extract pass/fail/skip counts from pytest output.

    Args:
        output: Full pytest stdout+stderr.
        result: PackageResult to populate.
    """
    import re

    for line in reversed(output.split("\n")):
        match = re.search(
            r"(\d+)\s+passed", line,
        )
        if match:
            result.passed = int(match.group(1))

        match = re.search(r"(\d+)\s+failed", line)
        if match:
            result.failed = int(match.group(1))

        match = re.search(r"(\d+)\s+error", line)
        if match:
            result.errors = int(match.group(1))

        match = re.search(r"(\d+)\s+skipped", line)
        if match:
            result.skipped = int(match.group(1))

        if result.passed > 0 or result.failed > 0:
            break


def _tail(text: str, n: int) -> str:
    """Get the last N lines of text.

    Args:
        text: Input text.
        n: Number of lines.

    Returns:
        str: Last N lines.
    """
    lines = text.strip().split("\n")
    return "\n".join(lines[-n:])
