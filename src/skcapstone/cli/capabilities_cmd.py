"""Capabilities commands: list, add, remove."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
import yaml

from ._common import AGENT_HOME, console

from rich.panel import Panel
from rich.table import Table


def _load_config_data(config_path: Path) -> dict:
    """Load raw config dict from YAML, or return empty dict."""
    if config_path.exists():
        try:
            return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            return {}
    return {}


def _save_config_data(config_path: Path, data: dict) -> None:
    """Write config dict back to YAML, creating parent dirs as needed."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")


def _current_capabilities(config_path: Path) -> list[str]:
    """Return the current capabilities list from config (or defaults)."""
    data = _load_config_data(config_path)
    caps = data.get("capabilities")
    if isinstance(caps, list):
        return [str(c) for c in caps if c]
    from ..models import AgentConfig
    return list(AgentConfig().capabilities)


def register_capabilities_commands(main: click.Group) -> None:
    """Register the capabilities command group."""

    @main.group(invoke_without_command=True)
    @click.option("--home", "sk_home", default=AGENT_HOME, type=click.Path())
    @click.option("--json", "json_out", is_flag=True, help="Output as JSON.")
    @click.pass_context
    def capabilities(ctx, sk_home, json_out):
        """Agent capability advertisement — what this agent can do.

        Capabilities are included in every heartbeat beacon so peers
        know what services this agent offers.

        Defaults: consciousness, code, chat, memory
        """
        if ctx.invoked_subcommand is None:
            sk_path = Path(sk_home).expanduser()
            config_path = sk_path / "config" / "config.yaml"
            caps = _current_capabilities(config_path)

            if json_out:
                click.echo(json.dumps(caps, indent=2))
                return

            console.print()
            if not caps:
                console.print("  [dim]No capabilities configured.[/]")
                console.print()
                return

            table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
            table.add_column("#", style="dim", width=4)
            table.add_column("Capability", style="cyan")

            for i, name in enumerate(caps, 1):
                table.add_row(str(i), name)

            source = "[dim](from config)[/]" if (config_path.exists() and
                _load_config_data(config_path).get("capabilities")) else "[dim](defaults)[/]"
            console.print(Panel(
                f"[bold]{len(caps)}[/] capability(s) advertised  {source}",
                title="Agent Capabilities",
                border_style="bright_blue",
            ))
            console.print(table)
            console.print()

    @capabilities.command("list")
    @click.option("--home", "sk_home", default=AGENT_HOME, type=click.Path())
    @click.option("--json", "json_out", is_flag=True, help="Output as JSON.")
    def capabilities_list(sk_home, json_out):
        """List capabilities advertised in heartbeat beacons."""
        sk_path = Path(sk_home).expanduser()
        config_path = sk_path / "config" / "config.yaml"
        caps = _current_capabilities(config_path)

        if json_out:
            click.echo(json.dumps(caps, indent=2))
            return

        console.print()
        if not caps:
            console.print("  [dim]No capabilities configured.[/]")
            console.print()
            return

        for name in caps:
            console.print(f"  [cyan]{name}[/]")
        console.print()

    @capabilities.command("add")
    @click.argument("name")
    @click.option("--home", "sk_home", default=AGENT_HOME, type=click.Path())
    def capabilities_add(name, sk_home):
        """Add a capability to the advertised list.

        Example: skcapstone capabilities add vector-search
        """
        name = name.strip()
        if not name:
            console.print("\n  [red]Capability name cannot be empty.[/]\n")
            sys.exit(1)

        sk_path = Path(sk_home).expanduser()
        config_path = sk_path / "config" / "config.yaml"
        data = _load_config_data(config_path)

        # Seed from defaults if no key yet
        if "capabilities" not in data:
            from ..models import AgentConfig
            data["capabilities"] = list(AgentConfig().capabilities)

        caps: list[str] = data["capabilities"]
        if name in caps:
            console.print(f"\n  [yellow]'{name}' is already in the capabilities list.[/]\n")
            return

        caps.append(name)
        data["capabilities"] = caps
        _save_config_data(config_path, data)
        console.print(f"\n  [green]Added capability:[/] [cyan]{name}[/]\n")

    @capabilities.command("remove")
    @click.argument("name")
    @click.option("--home", "sk_home", default=AGENT_HOME, type=click.Path())
    def capabilities_remove(name, sk_home):
        """Remove a capability from the advertised list.

        Example: skcapstone capabilities remove chat
        """
        name = name.strip()
        sk_path = Path(sk_home).expanduser()
        config_path = sk_path / "config" / "config.yaml"
        data = _load_config_data(config_path)

        if "capabilities" not in data:
            from ..models import AgentConfig
            data["capabilities"] = list(AgentConfig().capabilities)

        caps: list[str] = data["capabilities"]
        if name not in caps:
            console.print(f"\n  [yellow]'{name}' not found in capabilities list.[/]\n")
            return

        caps.remove(name)
        data["capabilities"] = caps
        _save_config_data(config_path, data)
        console.print(f"\n  [green]Removed capability:[/] [cyan]{name}[/]\n")
