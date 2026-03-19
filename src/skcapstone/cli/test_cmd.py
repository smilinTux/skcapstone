"""Test runner command — unified pytest across all ecosystem packages.

Usage:
    skcapstone test                     # run all packages
    skcapstone test --package skcomm   # run one package
    skcapstone test --fast              # stop on first failure
    skcapstone test --json-out          # machine-readable JSON output
    skcapstone test --verbose           # pass -v to pytest
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from ._common import console
from ..testrunner import ECOSYSTEM_PACKAGES, run_all_tests, TestReport, PackageResult


def _monorepo_root() -> Path:
    """Locate the monorepo root (parent of the skcapstone package dir).

    Walks up from this file until we find a directory that contains
    the skcapstone sub-package, then returns its parent.

    Returns:
        Path to the monorepo root.
    """
    # src/skcapstone/cli/test_cmd.py → go up 4 levels
    candidate = Path(__file__).resolve().parent.parent.parent.parent.parent
    # candidate is now smilintux-org/skcapstone/src → go one more up to skcapstone pkg root,
    # then one more up to monorepo root
    # Path layout: monorepo/skcapstone/src/skcapstone/cli/test_cmd.py
    # parents[0] = cli/, [1] = skcapstone (pkg), [2] = src/, [3] = skcapstone (repo), [4] = monorepo
    monorepo = Path(__file__).resolve().parents[4]
    if (monorepo / "skcapstone").is_dir():
        return monorepo
    # Fallback: cwd
    return Path.cwd()


def _render_table(report: TestReport) -> None:
    """Print a Rich table summarising per-package results.

    Args:
        report: Completed test report.
    """
    from rich.table import Table
    from rich.panel import Panel

    table = Table(title="Ecosystem Test Results", show_lines=False, expand=True)
    table.add_column("Package", style="bold", min_width=16)
    table.add_column("Status", min_width=10)
    table.add_column("Passed", justify="right", min_width=7)
    table.add_column("Failed", justify="right", min_width=7)
    table.add_column("Errors", justify="right", min_width=7)
    table.add_column("Skipped", justify="right", min_width=7)
    table.add_column("Duration", justify="right", min_width=9)

    for r in report.results:
        if not r.available:
            table.add_row(
                r.name,
                "[dim]N/A[/]",
                "-", "-", "-", "-",
                "[dim]—[/]",
            )
            continue

        if r.success:
            status = "[bold green]PASS[/]"
        else:
            status = "[bold red]FAIL[/]"

        failed_str = f"[red]{r.failed}[/]" if r.failed else "0"
        errors_str = f"[red]{r.errors}[/]" if r.errors else "0"
        skipped_str = f"[yellow]{r.skipped}[/]" if r.skipped else "0"

        table.add_row(
            r.name,
            status,
            str(r.passed),
            failed_str,
            errors_str,
            skipped_str,
            f"{r.duration_s:.1f}s",
        )

    console.print()
    console.print(table)

    # Footer summary
    total_pass = report.total_passed
    total_fail = report.total_failed
    total_err = report.total_errors
    pkgs_tested = report.packages_tested
    overall_duration = f"{report.duration_s:.1f}s"

    if report.all_passed:
        summary_color = "green"
        verdict = "ALL PASSED"
    else:
        summary_color = "red"
        verdict = "FAILURES DETECTED"

    console.print(
        Panel(
            f"[bold {summary_color}]{verdict}[/]  "
            f"[green]{total_pass} passed[/]  "
            + (f"[red]{total_fail} failed[/]  " if total_fail else "")
            + (f"[red]{total_err} errors[/]  " if total_err else "")
            + f"across {pkgs_tested} package(s)  "
            f"in {overall_duration}",
            border_style=summary_color,
            expand=False,
        )
    )
    console.print()


def _render_failures(report: TestReport) -> None:
    """Print abbreviated pytest output for any failed package.

    Args:
        report: Completed test report.
    """
    for r in report.results:
        if r.available and not r.success and r.output.strip():
            console.print(f"\n[bold red]── {r.name} output ──[/]\n")
            console.print(r.output)


def register_test_commands(main: click.Group) -> None:
    """Register the `skcapstone test` command.

    Args:
        main: Root Click group.
    """

    @main.command("test")
    @click.option(
        "--package", "-p",
        "packages",
        multiple=True,
        metavar="NAME",
        help="Restrict to one or more packages (repeat for multiple).",
    )
    @click.option(
        "--fast", is_flag=True,
        help="Stop after the first failing package.",
    )
    @click.option(
        "--verbose", "-v", is_flag=True,
        help="Pass -v to pytest for detailed output.",
    )
    @click.option(
        "--json-out", is_flag=True,
        help="Emit machine-readable JSON instead of a Rich table.",
    )
    @click.option(
        "--timeout", default=120, show_default=True, type=int,
        help="Per-package timeout in seconds.",
    )
    @click.option(
        "--root", default=None, type=click.Path(),
        help="Override monorepo root path (auto-detected by default).",
    )
    @click.option(
        "--show-output", is_flag=True,
        help="Print pytest output for failing packages.",
    )
    def test_cmd(
        packages: tuple[str, ...],
        fast: bool,
        verbose: bool,
        json_out: bool,
        timeout: int,
        root: str | None,
        show_output: bool,
    ) -> None:
        """Run pytest across all ecosystem packages and show a summary table.

        Discovers packages in the monorepo (skcapstone, capauth, skcomm,
        skchat, skmemory, cloud9) and runs their test suites in
        sequence, then renders a consolidated Rich table.

        \b
        Examples:
            skcapstone test
            skcapstone test --package skcomm
            skcapstone test -p skcomm -p skchat
            skcapstone test --fast --verbose
            skcapstone test --json-out | jq .
        """
        monorepo = Path(root).expanduser() if root else _monorepo_root()

        pkg_filter = list(packages) if packages else None

        valid_names = {p["name"] for p in ECOSYSTEM_PACKAGES}
        if pkg_filter:
            invalid = [n for n in pkg_filter if n not in valid_names]
            if invalid:
                console.print(
                    f"[red]Unknown package(s): {', '.join(invalid)}[/]\n"
                    f"Valid: {', '.join(sorted(valid_names))}"
                )
                sys.exit(1)

        if not json_out:
            names_str = ", ".join(pkg_filter) if pkg_filter else "all packages"
            console.print(f"\n  [cyan]Running tests for:[/] {names_str}")
            console.print(f"  [dim]Monorepo root: {monorepo}[/]\n")

        report = run_all_tests(
            monorepo_root=monorepo,
            packages=pkg_filter,
            fail_fast=fast,
            verbose=verbose,
            timeout=timeout,
        )

        if json_out:
            click.echo(json.dumps(report.to_dict(), indent=2))
            sys.exit(0 if report.all_passed else 1)

        _render_table(report)

        if show_output:
            _render_failures(report)

        sys.exit(0 if report.all_passed else 1)
