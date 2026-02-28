"""Crush (charmbracelet/crush) integration commands.

Subcommands:
    skcapstone crush setup   — install config + soul instructions
    skcapstone crush config  — print the generated crush.json
    skcapstone crush status  — show installation status
"""

from __future__ import annotations

import json

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


def register_crush_commands(main: click.Group) -> None:
    """Register the crush command group."""

    @main.group()
    def crush():
        """Crush terminal AI client integration.

        Wire the charmbracelet/crush TUI to skcapstone MCP and
        load your soul blueprint as its system instructions.

        https://github.com/charmbracelet/crush
        """

    @crush.command("setup")
    @click.option(
        "--overwrite",
        is_flag=True,
        default=False,
        help="Overwrite existing crush config files.",
    )
    def crush_setup(overwrite: bool) -> None:
        """Set up Crush: write crush.json + instructions.md.

        Writes:
          ~/.config/crush/crush.json     — MCP wiring + permissions
          ~/.config/crush/instructions.md — soul blueprint as system prompt
        """
        from ..crush_integration import setup_crush

        console.print()
        with console.status("  Configuring Crush…", spinner="dots"):
            result = setup_crush(overwrite=overwrite)

        t = Table.grid(padding=(0, 2))
        t.add_column(style="dim", width=20)
        t.add_column()

        if result["installed"]:
            t.add_row("Binary", f"[green]{result['binary_path']}[/]")
        else:
            t.add_row("Binary", "[yellow]not found[/]")
            if result.get("install_hint"):
                t.add_row("Install with", f"[cyan]{result['install_hint']}[/]")

        t.add_row("Config", f"[dim]{result['config_path']}[/]")
        t.add_row("Instructions", f"[dim]{result['instructions_path']}[/]")

        console.print(
            Panel(
                t,
                title="[bold]Crush Setup Complete[/]",
                border_style="bright_blue",
            )
        )

        console.print()
        console.print(
            "  [dim]Run crush in your project directory to start coding with your sovereign agent.[/]"
        )
        if not result["installed"]:
            console.print()
            hint = result.get("install_hint", "go install github.com/charmbracelet/crush@latest")
            console.print(f"  [bold]Install crush:[/]  [cyan]{hint}[/]")
        console.print()

    @crush.command("config")
    @click.option(
        "--json-only",
        "json_only",
        is_flag=True,
        default=False,
        help="Print raw JSON without formatting.",
    )
    def crush_config(json_only: bool) -> None:
        """Print the crush.json config that will be written."""
        from ..crush_integration import generate_crush_config

        config = generate_crush_config()
        if json_only:
            click.echo(json.dumps(config, indent=2))
        else:
            console.print()
            console.print_json(json.dumps(config, indent=2))
            console.print()
            console.print(
                f"  [dim]Write this to [cyan]~/.config/crush/crush.json[/] "
                f"with:  skcapstone crush setup[/]"
            )
            console.print()

    @crush.command("status")
    def crush_status() -> None:
        """Show Crush installation and config status."""
        from ..crush_integration import (
            find_crush_binary,
            get_install_hint,
            is_crush_installed,
        )
        from pathlib import Path

        crush_config_dir = Path("~/.config/crush").expanduser()
        crush_json = crush_config_dir / "crush.json"
        instructions_md = crush_config_dir / "instructions.md"

        console.print()
        t = Table(
            title="Crush Status",
            border_style="bright_blue",
            show_header=True,
        )
        t.add_column("Item", style="bold", width=22)
        t.add_column("Status")
        t.add_column("Detail", style="dim")

        # Binary
        binary = find_crush_binary()
        if binary:
            t.add_row("Binary", "[green]FOUND[/]", str(binary))
        else:
            hint = get_install_hint()
            t.add_row("Binary", "[yellow]MISSING[/]", hint)

        # crush.json
        if crush_json.exists():
            try:
                data = json.loads(crush_json.read_text(encoding="utf-8"))
                mcp_count = len(data.get("mcp", {}))
                t.add_row(
                    "crush.json",
                    "[green]OK[/]",
                    f"{mcp_count} MCP server(s) — {str(crush_json)}",
                )
            except Exception:
                t.add_row("crush.json", "[red]CORRUPT[/]", str(crush_json))
        else:
            t.add_row("crush.json", "[yellow]MISSING[/]", "run: skcapstone crush setup")

        # instructions.md
        if instructions_md.exists():
            size = instructions_md.stat().st_size
            t.add_row("instructions.md", "[green]OK[/]", f"{size} bytes — {str(instructions_md)}")
        else:
            t.add_row(
                "instructions.md",
                "[yellow]MISSING[/]",
                "run: skcapstone crush setup",
            )

        console.print(t)
        console.print()
