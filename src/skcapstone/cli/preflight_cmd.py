"""CLI command: skcapstone preflight — run daemon startup checks."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ._common import AGENT_HOME, console


def register_preflight_commands(main: click.Group) -> None:
    """Register the preflight command."""

    @main.command("preflight")
    @click.option("--home", default=AGENT_HOME, type=click.Path(),
                  help="Agent home directory.")
    @click.option("--json-out", is_flag=True, help="Output as JSON.")
    def preflight_cmd(home: str, json_out: bool):
        """Run daemon preflight checks.

        Verifies that the environment is ready for daemon startup:
        Python version, required packages, Ollama, PGP identity,
        home directory structure, consciousness config, and disk space.

        Exits with code 0 if all critical checks pass, 1 otherwise.

        Examples:

            skcapstone preflight

            skcapstone preflight --json-out
        """
        import json
        from ..preflight import PreflightChecker

        checker = PreflightChecker(home=Path(home).expanduser())
        summary = checker.run_all()

        if json_out:
            click.echo(json.dumps(summary, indent=2))
            sys.exit(0 if summary["ok"] else 1)

        # Colored table output
        console.print()
        console.print("[bold]Daemon Preflight Checks[/]")
        console.print()

        status_styles = {
            "ok": ("[green]  OK  [/]", "green"),
            "warn": ("[yellow] WARN [/]", "yellow"),
            "fail": ("[bold red] FAIL [/]", "red"),
        }

        for check in summary["checks"]:
            badge, color = status_styles.get(check["status"], ("[dim]  ?  [/]", "white"))
            name = check["name"].ljust(12)
            msg = check["message"]
            console.print(f"  {badge}  [{color}]{name}[/]  {msg}")

        console.print()

        warnings = summary["warnings"]
        failures = summary["failures"]
        critical = summary["critical_failures"]

        if summary["ok"]:
            if warnings or failures:
                console.print(
                    f"[yellow]Preflight passed with {warnings} warning(s) "
                    f"and {failures} non-critical failure(s).[/]"
                )
            else:
                console.print("[bold green]All preflight checks passed.[/]")
        else:
            console.print(
                f"[bold red]Preflight FAILED — {critical} critical failure(s).[/] "
                "Fix the issues above before starting the daemon."
            )

        console.print()
        sys.exit(0 if summary["ok"] else 1)
