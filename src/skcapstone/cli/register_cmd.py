"""Register command — auto-register SK* skills and MCP servers.

Detects the user's environments (OpenClaw, Claude Code, Cursor, VS Code,
OpenCode CLI, mcporter) and registers SKILL.md symlinks + MCP server entries.

Commands:
    skcapstone register              — register all SK* packages
    skcapstone register --dry-run    — show what would be done
    skcapstone register --env claude-code  — target specific environment
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import click

from ._common import AGENT_HOME, console


def register_register_commands(main: click.Group) -> None:
    """Register the 'register' command on the main CLI group."""

    @main.command()
    @click.option(
        "--workspace",
        default=None,
        help="Workspace root directory (default: ~/clawd/).",
        type=click.Path(),
    )
    @click.option(
        "--env",
        "target_env",
        default=None,
        help="Target a specific environment (e.g. claude-code, cursor, mcporter).",
    )
    @click.option(
        "--dry-run",
        is_flag=True,
        default=False,
        help="Show what would be done without making changes.",
    )
    def register(
        workspace: Optional[str],
        target_env: Optional[str],
        dry_run: bool,
    ) -> None:
        """Register all SK* skills and MCP servers in detected environments.

        Auto-detects your development environments (Claude Code, Cursor,
        VS Code, OpenClaw, OpenCode, mcporter) and ensures all SK* skill
        manifests and MCP server entries are properly configured.

        Examples:

          skcapstone register                  # auto-detect and register
          skcapstone register --dry-run        # preview what would happen
          skcapstone register --env claude-code # target Claude Code only
        """
        from skmemory.register import detect_environments
        from skcapstone.register import register_all

        workspace_path = Path(workspace).expanduser() if workspace else None
        environments = [target_env] if target_env else None

        console.print()
        console.print("[bold cyan]SK* Registration[/]")
        console.print()

        # Show detected environments
        detected = detect_environments()
        console.print("  [bold]Detected environments:[/]")
        for env in detected:
            marker = "[green]●[/]" if (environments is None or env in environments) else "[dim]○[/]"
            console.print(f"    {marker} {env}")
        if not detected:
            console.print("    [dim]None detected[/]")
            console.print()
            console.print("  [yellow]Tip:[/] Install OpenClaw, Claude Code, or Cursor to enable registration.")
        console.print()

        if dry_run:
            console.print("  [yellow]Dry run — no changes will be made.[/]")
            console.print()

        # Run registration
        results = register_all(
            workspace=workspace_path,
            environments=environments,
            dry_run=dry_run,
        )

        # Display results
        from rich.table import Table

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Package", style="cyan")
        table.add_column("Skill", style="dim")
        table.add_column("MCP")
        table.add_column("OpenClaw Plugin")

        for name, pkg_result in results.get("packages", {}).items():
            skill_info = pkg_result.get("skill", {})
            skill_action = skill_info.get("action", "—")

            if skill_action == "created":
                skill_str = "[green]created[/]"
            elif skill_action == "exists":
                skill_str = "[dim]exists[/]"
            elif skill_action == "error":
                skill_str = f"[red]{skill_info.get('error', 'error')}[/]"
            elif skill_action == "dry-run":
                skill_str = "[yellow]would create[/]"
            else:
                skill_str = f"[dim]{skill_action}[/]"

            mcp_info = pkg_result.get("mcp", {})
            if not mcp_info:
                mcp_str = "[dim]—[/]"
            elif isinstance(mcp_info, dict):
                parts = []
                for env_name, action in mcp_info.items():
                    if action == "created":
                        parts.append(f"[green]{env_name}[/]")
                    elif action == "exists":
                        parts.append(f"[dim]{env_name}[/]")
                    elif action == "dry-run":
                        parts.append(f"[yellow]{env_name}[/]")
                    else:
                        parts.append(f"{env_name}:{action}")
                mcp_str = ", ".join(parts) if parts else "[dim]—[/]"
            else:
                mcp_str = str(mcp_info)

            plugin_action = pkg_result.get("openclaw_plugin", "")
            if plugin_action == "created":
                plugin_str = "[green]created[/]"
            elif plugin_action == "exists":
                plugin_str = "[dim]exists[/]"
            elif plugin_action == "dry-run":
                plugin_str = "[yellow]would create[/]"
            elif plugin_action and plugin_action.startswith("error"):
                plugin_str = f"[red]{plugin_action}[/]"
            elif not plugin_action:
                plugin_str = "[dim]—[/]"
            else:
                plugin_str = f"[dim]{plugin_action}[/]"

            table.add_row(name, skill_str, mcp_str, plugin_str)

        console.print(table)
        console.print()

        if dry_run:
            console.print("  [yellow]No changes made (dry run).[/]")
        else:
            console.print("  [green]Registration complete.[/]")
        console.print()
