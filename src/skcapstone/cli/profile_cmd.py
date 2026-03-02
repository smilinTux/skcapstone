"""Profile commands: list, show, stale."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import click

from ._common import AGENT_HOME, console


# Number of days before a profile is considered stale.
_STALE_DAYS = 90


def _load_profiles_path(home: str) -> Path | None:
    """Resolve the agent-level model_profiles.yaml override if it exists.

    Args:
        home: Agent home directory path.

    Returns:
        Path to agent-level profiles YAML, or None to use bundled defaults.
    """
    agent_override = Path(home).expanduser() / "config" / "model_profiles.yaml"
    if agent_override.exists():
        return agent_override
    return None


def _get_adapter(home: str):
    """Return a PromptAdapter, preferring agent-level profiles.

    Args:
        home: Agent home directory.

    Returns:
        PromptAdapter instance with profiles loaded.
    """
    from ..prompt_adapter import PromptAdapter

    profiles_path = _load_profiles_path(home)
    return PromptAdapter(profiles_path)


def _parse_last_updated(value: str) -> date | None:
    """Parse a last_updated string to a date object.

    Args:
        value: ISO-8601 date string (e.g. "2026-03-02").

    Returns:
        date object or None if the string is empty / unparseable.
    """
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def register_profile_commands(main: click.Group) -> None:
    """Register the ``profile`` command group on the main CLI."""

    @main.group()
    def profile():
        """Model profile management — inspect prompt-formatting profiles.

        Model profiles define how prompts are formatted for each LLM family:
        system-prompt placement, structure format, thinking mode, temperature
        defaults, and more.

        The bundled profiles live in ``data/model_profiles.yaml``.  To
        override any entry, place a custom copy at::

            ~/.skcapstone/agents/<name>/config/model_profiles.yaml
        """

    # ------------------------------------------------------------------
    # profile list
    # ------------------------------------------------------------------

    @profile.command("list")
    @click.option(
        "--home", default=AGENT_HOME, type=click.Path(),
        help="Agent home directory (used to resolve overrides).",
    )
    @click.option("--json", "json_out", is_flag=True, help="Output raw JSON.")
    def profile_list(home: str, json_out: bool) -> None:
        """List all loaded model profiles.

        Profiles are shown in the order they are matched (first match wins).
        The agent-level override (if present) is merged on top of the
        bundled defaults.

        Examples:

            skcapstone profile list

            skcapstone profile list --json
        """
        from rich.panel import Panel
        from rich.table import Table

        adapter = _get_adapter(home)
        profiles = adapter.profiles

        if not profiles:
            console.print("\n  [dim]No profiles loaded.[/]\n")
            sys.exit(1)

        if json_out:
            rows = [p.model_dump() for p in profiles]
            click.echo(json.dumps(rows, indent=2))
            return

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Family", style="cyan")
        table.add_column("Pattern", style="dim")
        table.add_column("Sys-prompt mode")
        table.add_column("Format")
        table.add_column("Thinking")
        table.add_column("Updated", style="dim")

        for p in profiles:
            thinking = p.thinking_mode if p.thinking_enabled else "off"
            table.add_row(
                p.family,
                p.model_pattern,
                p.system_prompt_mode,
                p.structure_format,
                thinking,
                p.last_updated or "[dim]unknown[/]",
            )

        label = f"[bold]{len(profiles)}[/] model profile(s) loaded"
        console.print()
        console.print(
            Panel(label, title="Model Profiles", border_style="bright_blue")
        )
        console.print(table)
        console.print()

    # ------------------------------------------------------------------
    # profile show
    # ------------------------------------------------------------------

    @profile.command("show")
    @click.argument("model")
    @click.option(
        "--home", default=AGENT_HOME, type=click.Path(),
        help="Agent home directory (used to resolve overrides).",
    )
    @click.option("--json", "json_out", is_flag=True, help="Output raw JSON.")
    def profile_show(model: str, home: str, json_out: bool) -> None:
        """Show the profile matched for MODEL.

        MODEL is matched against all profile ``model_pattern`` regexes.
        If nothing matches, the generic fallback profile is shown.

        Examples:

            skcapstone profile show claude-opus-4-5

            skcapstone profile show grok-3

            skcapstone profile show deepseek-r1-70b --json
        """
        from rich.panel import Panel
        from rich.table import Table

        adapter = _get_adapter(home)
        p = adapter.resolve_profile(model)

        if json_out:
            click.echo(json.dumps(p.model_dump(), indent=2))
            return

        console.print()
        console.print(
            Panel(
                f"Profile for [bold cyan]{model}[/] → family [bold]{p.family}[/]",
                border_style="cyan",
                padding=(0, 2),
            )
        )

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Field", style="bold dim", min_width=28)
        table.add_column("Value")

        rows = [
            ("model_pattern", p.model_pattern),
            ("family", p.family),
            ("system_prompt_mode", p.system_prompt_mode),
            ("structure_format", p.structure_format),
            ("thinking_enabled", str(p.thinking_enabled)),
            ("thinking_mode", p.thinking_mode),
            ("thinking_budget_tokens", str(p.thinking_budget_tokens)),
            ("default_temperature",
             str(p.default_temperature) if p.default_temperature is not None else "[dim]—[/]"),
            ("code_temperature",
             str(p.code_temperature) if p.code_temperature is not None else "[dim]—[/]"),
            ("reasoning_temperature",
             str(p.reasoning_temperature) if p.reasoning_temperature is not None else "[dim]—[/]"),
            ("max_system_tokens", str(p.max_system_tokens)),
            ("tool_format", p.tool_format),
            ("no_few_shot", str(p.no_few_shot)),
            ("no_cot_instructions", str(p.no_cot_instructions)),
            ("last_updated", p.last_updated or "[dim]unknown[/]"),
            ("source_url", p.source_url or "[dim]—[/]"),
            ("notes", p.notes or "[dim]—[/]"),
        ]

        for field, value in rows:
            table.add_row(field, value)

        console.print(table)
        console.print()

    # ------------------------------------------------------------------
    # profile stale
    # ------------------------------------------------------------------

    @profile.command("stale")
    @click.option(
        "--home", default=AGENT_HOME, type=click.Path(),
        help="Agent home directory (used to resolve overrides).",
    )
    @click.option(
        "--days", default=_STALE_DAYS, type=int, show_default=True,
        help="Number of days before a profile is considered stale.",
    )
    @click.option("--json", "json_out", is_flag=True, help="Output raw JSON.")
    def profile_stale(home: str, days: int, json_out: bool) -> None:
        """Show profiles older than DAYS days (default: 90).

        A profile is stale when its ``last_updated`` date is more than
        ``--days`` days in the past, or when ``last_updated`` is missing.

        Stale profiles may have outdated guidance — consider refreshing
        them against the latest provider documentation.

        Examples:

            skcapstone profile stale

            skcapstone profile stale --days 30

            skcapstone profile stale --json
        """
        from rich.panel import Panel
        from rich.table import Table

        adapter = _get_adapter(home)
        cutoff = date.today() - timedelta(days=days)

        stale = []
        for p in adapter.profiles:
            updated = _parse_last_updated(p.last_updated)
            if updated is None or updated < cutoff:
                stale.append((p, updated))

        if json_out:
            rows = []
            for p, updated in stale:
                d = p.model_dump()
                d["_days_old"] = (
                    (date.today() - updated).days if updated else None
                )
                rows.append(d)
            click.echo(json.dumps(rows, indent=2))
            return

        if not stale:
            console.print(
                f"\n  [bold green]✓[/] All profiles updated within {days} days.\n"
            )
            return

        label = (
            f"[bold]{len(stale)}[/] stale profile(s) "
            f"(older than {days} days — cutoff [dim]{cutoff}[/])"
        )
        console.print()
        console.print(
            Panel(label, title="Stale Profiles", border_style="yellow")
        )

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Family", style="cyan")
        table.add_column("Pattern", style="dim")
        table.add_column("Last Updated", style="yellow")
        table.add_column("Age (days)", style="red")

        for p, updated in stale:
            age = str((date.today() - updated).days) if updated else "[dim]unknown[/]"
            table.add_row(
                p.family,
                p.model_pattern,
                p.last_updated or "[dim]missing[/]",
                age,
            )

        console.print(table)
        console.print()
