"""CLI command group: ``skcapstone selftest`` - post-resume health self-test.

Provides ``skcapstone selftest post-resume``, an automated, read-only check
that verifies the sovereign stack is healthy after the machine wakes from
suspend. It reuses the existing doctor / daemon / coordination health machinery
and exits non-zero if any critical check fails, so it can be wired into a
systemd suspend/resume hook.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from ._common import AGENT_HOME, console


def register_selftest_commands(main: click.Group) -> None:
    """Register the ``selftest`` command group on the main CLI group."""

    @main.group("selftest")
    def selftest() -> None:
        """Automated stack self-tests (read-only health verification)."""

    @selftest.command("post-resume")
    @click.option("--home", default=AGENT_HOME, type=click.Path(), help="Agent home directory.")
    @click.option(
        "--json-out", "json_out", is_flag=True, help="Output the structured report as JSON."
    )
    @click.option(
        "--alert/--no-alert",
        "alert",
        default=None,
        help="Force alerting on/off, overriding selftest.yaml. "
        "Alerts fire only on a critical failure.",
    )
    @click.option(
        "--quiet", "-q", is_flag=True, help="Suppress the table; only set the exit code."
    )
    def post_resume_cmd(home: str, json_out: bool, alert, quiet: bool):
        """Verify the sovereign stack is healthy after a resume.

        Runs a suite of read-only checks (daemon alive, memory backend,
        coordination board, comms transport, identity/token validity, plus
        resume-specific clock-skew and network reachability), reusing the
        existing ``skcapstone doctor`` diagnostics. Reports a structured
        pass/fail per check and an overall status.

        Exit code is 0 when no critical check fails, 1 otherwise, so this can be
        dropped straight into a systemd suspend/resume hook. With
        ``--alert`` (or ``alert_enabled: true`` in selftest.yaml) a critical
        failure emits a desktop / alert notification.

        Examples:

            skcapstone selftest post-resume

            skcapstone selftest post-resume --json-out

            skcapstone selftest post-resume --alert --quiet
        """
        from ..post_resume import SelfTestConfig, run_post_resume_selftest

        home_path = Path(home).expanduser()
        cfg = SelfTestConfig.load(home_path)
        if alert is not None:
            cfg.alert_enabled = bool(alert)

        report = run_post_resume_selftest(home_path, cfg)

        if json_out:
            click.echo(json.dumps(report.to_dict(), indent=2))
            sys.exit(report.exit_code)

        if not quiet:
            _render_table(report)

        sys.exit(report.exit_code)


def _render_table(report) -> None:
    """Print a colored summary of a self-test report."""
    console.print()
    console.print("[bold]Post-Resume Self-Test[/]")
    console.print()

    status_styles = {
        "pass": ("[green] PASS [/]", "green"),
        "warn": ("[yellow] WARN [/]", "yellow"),
        "fail": ("[bold red] FAIL [/]", "red"),
        "skip": ("[dim] SKIP [/]", "dim"),
    }

    for check in report.checks:
        badge, color = status_styles.get(check.status, ("[dim]  ?  [/]", "white"))
        name = check.name.ljust(20)
        line = f"  {badge}  [{color}]{name}[/]  {check.detail}"
        console.print(line)
        if check.failed and check.fix:
            console.print(f"        [dim]fix: {check.fix}[/]")

    console.print()

    counts = report.to_dict()["counts"]
    summary = (
        f"{counts['passed']} passed, {counts['warnings']} warning(s), "
        f"{counts['failures']} failure(s) "
        f"({counts['critical_failures']} critical), {counts['skipped']} skipped"
    )
    if report.passed:
        console.print(f"[bold green]Self-test PASSED.[/] {summary}")
    else:
        console.print(f"[bold red]Self-test FAILED.[/] {summary}")
    if report.alerted:
        console.print("  [dim]alert emitted[/]")
    console.print()
