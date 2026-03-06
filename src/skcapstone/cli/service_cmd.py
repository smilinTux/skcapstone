"""CLI command: skcapstone service check — service health checks."""

from __future__ import annotations

import json

import click
from rich.table import Table

from ._common import console


def register_service_commands(main: click.Group) -> None:
    """Register the ``service`` command group on the main CLI."""

    @main.group()
    def service():
        """Service infrastructure commands."""

    @service.command("check")
    @click.option("--json-out", is_flag=True, help="Output as machine-readable JSON.")
    def service_check(json_out: bool):
        """Check health of all services in the sovereign stack.

        Pings SKVector (Qdrant), SKGraph (FalkorDB), Syncthing,
        skcapstone daemon, and skchat daemon.  Reports status,
        latency, and any errors.
        """
        from ..service_health import check_all_services

        results = check_all_services()

        if json_out:
            up = sum(1 for r in results if r["status"] == "up")
            down = sum(1 for r in results if r["status"] == "down")
            data = {
                "summary": {
                    "total": len(results),
                    "up": up,
                    "down": down,
                    "unknown": len(results) - up - down,
                },
                "services": results,
            }
            click.echo(json.dumps(data, indent=2))
            return

        console.print()

        table = Table(
            show_header=True, header_style="bold", box=None, padding=(0, 2),
        )
        table.add_column("Service", style="cyan", no_wrap=True)
        table.add_column("URL", style="dim", no_wrap=True)
        table.add_column("Status")
        table.add_column("Latency", justify="right", style="dim")
        table.add_column("Version", style="dim")
        table.add_column("Error", style="dim")

        status_styles = {
            "up": "[green]UP[/]",
            "down": "[red]DOWN[/]",
            "unknown": "[yellow]UNKNOWN[/]",
        }

        for r in results:
            status_str = status_styles.get(r["status"], r["status"])
            latency_str = f"{r['latency_ms']:.0f}ms" if r["latency_ms"] else "-"
            version_str = str(r["version"]) if r["version"] else "-"
            error_str = r["error"][:50] if r["error"] else "-"
            table.add_row(
                r["name"], r["url"], status_str,
                latency_str, version_str, error_str,
            )

        console.print(table)

        up_count = sum(1 for r in results if r["status"] == "up")
        down_count = sum(1 for r in results if r["status"] == "down")
        total = len(results)

        console.print()
        if down_count == 0:
            console.print(f"  [green]All {up_count}/{total} reachable services are healthy.[/]")
        else:
            console.print(
                f"  [green]{up_count}[/] up, "
                f"[red]{down_count}[/] down "
                f"out of {total} services."
            )
        console.print()
