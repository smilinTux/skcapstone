"""Config commands: validate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from ._common import AGENT_HOME, console


def register_config_commands(main: click.Group) -> None:
    """Register the ``config`` command group on the main CLI."""

    @main.group()
    def config():
        """Config management — validate and inspect agent configuration."""

    @config.command("validate")
    @click.option(
        "--home", default=AGENT_HOME, type=click.Path(),
        help="Agent home directory.",
    )
    @click.option("--json-out", is_flag=True, help="Output as machine-readable JSON.")
    @click.option(
        "--strict", is_flag=True,
        help="Treat warnings as errors (non-zero exit when warnings present).",
    )
    def validate(home: str, json_out: bool, strict: bool) -> None:
        """Validate all agent config files.

        Checks consciousness.yaml, model_profiles.yaml, identity.json,
        and soul blueprints. Reports schema errors with line numbers.

        Exits 0 when all configs are valid (warnings do not cause failure
        unless --strict is set).  Exits 1 when any errors are found or
        --strict is set and warnings are present.
        """
        from ..config_validator import validate_all

        home_path = Path(home).expanduser()
        report = validate_all(home_path)

        if json_out:
            _json_output(report, strict)
            return

        _rich_output(report, strict)

        has_errors = report.total_errors > 0 or (strict and report.total_warnings > 0)
        if has_errors:
            sys.exit(1)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _json_output(report: object, strict: bool) -> None:  # type: ignore[type-arg]
    """Emit machine-readable JSON to stdout."""
    from ..config_validator import ConfigValidationReport

    r: ConfigValidationReport = report  # type: ignore[assignment]
    data = {
        "valid": r.is_valid and not (strict and r.total_warnings > 0),
        "total_errors": r.total_errors,
        "total_warnings": r.total_warnings,
        "files": [
            {
                "name": fr.config_name,
                "path": str(fr.file_path),
                "found": fr.found,
                "valid": fr.is_valid,
                "issues": [
                    {
                        "severity": i.severity,
                        "message": i.message,
                        "field": i.field,
                        "line": i.line,
                    }
                    for i in fr.issues
                ],
            }
            for fr in r.results
        ],
    }
    click.echo(json.dumps(data, indent=2))


def _rich_output(report: object, strict: bool) -> None:  # type: ignore[type-arg]
    """Emit formatted Rich output to the terminal."""
    from ..config_validator import ConfigValidationReport
    from rich.panel import Panel

    r: ConfigValidationReport = report  # type: ignore[assignment]

    console.print()
    console.print(Panel(
        "[bold]Config Validation[/]",
        border_style="cyan",
        padding=(0, 2),
    ))
    console.print()

    for fr in r.results:
        if not fr.found:
            icon, status = "[yellow]~[/]", "[yellow]NOT FOUND[/]"
        elif fr.errors:
            n = len(fr.errors)
            icon = "[red]✗[/]"
            status = f"[red]FAIL  {n} error{'s' if n != 1 else ''}[/]"
        elif fr.warnings:
            n = len(fr.warnings)
            icon = "[yellow]~[/]"
            status = f"[yellow]OK  {n} warning{'s' if n != 1 else ''}[/]"
        else:
            icon, status = "[green]✓[/]", "[green]OK[/]"

        console.print(f"  {icon} [bold]{fr.config_name}[/]  {status}")
        console.print(f"     [dim]{fr.file_path}[/]")

        for issue in fr.issues:
            color = "red" if issue.severity == "error" else "yellow"
            loc = f"  line {issue.line}" if issue.line else ""
            fld = f"  [{issue.field}]" if issue.field else ""
            console.print(
                f"       [{color}]{issue.severity.upper()}[/]"
                f"[dim]{fld}{loc}[/]  {issue.message}"
            )

        console.print()

    # Summary line
    errors = r.total_errors
    warnings = r.total_warnings
    if errors == 0 and warnings == 0:
        console.print("  [bold green]✓ All configs valid.[/]")
    elif errors == 0:
        detail = f"{warnings} warning{'s' if warnings != 1 else ''}"
        console.print(
            f"  [bold yellow]~ {detail}.[/]  Configs are functional."
        )
    else:
        err_detail = f"{errors} error{'s' if errors != 1 else ''}"
        warn_detail = (
            f", {warnings} warning{'s' if warnings != 1 else ''}"
            if warnings else ""
        )
        console.print(
            f"  [bold red]✗ {err_detail}{warn_detail}[/]  — fix before running the agent."
        )

    console.print()
