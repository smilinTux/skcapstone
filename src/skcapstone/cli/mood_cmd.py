"""Mood command: display the agent's current emotional state."""

from __future__ import annotations

import json
from pathlib import Path

import click

from ._common import AGENT_HOME, console


def register_mood_commands(main: click.Group) -> None:
    """Register the ``skcapstone mood`` command."""

    @main.command("mood")
    @click.option(
        "--home", default=AGENT_HOME, type=click.Path(),
        help="Agent home directory.",
    )
    @click.option("--json-out", is_flag=True, help="Output raw JSON.")
    @click.option(
        "--update", is_flag=True,
        help="Refresh from persisted daily metrics before displaying.",
    )
    def mood_cmd(home: str, json_out: bool, update: bool) -> None:
        """Show the agent's current emotional state.

        Mood is derived from three interaction pattern factors:

        \b
          Success   — response success rate (happy / frustrated)
          Social    — message frequency     (social / isolated)
          Stress    — error rate            (calm / stressed)
        """
        from ..mood import MoodTracker

        home_path = Path(home).expanduser()
        tracker = MoodTracker(home=home_path)

        if update:
            try:
                from ..metrics import ConsciousnessMetrics
                metrics = ConsciousnessMetrics(home=home_path, persist_interval=0)
                tracker.update_from_metrics(metrics)
            except Exception as exc:
                if not json_out:
                    console.print(f"[yellow]Warning: could not refresh metrics: {exc}[/]")

        snap = tracker.snapshot

        if json_out:
            click.echo(snap.model_dump_json(indent=2))
            return

        _print_mood(snap)


# ---------------------------------------------------------------------------
# Rich rendering
# ---------------------------------------------------------------------------

_SUMMARY_COLORS: dict[str, str] = {
    "flourishing": "bright_green",
    "happy": "green",
    "content": "cyan",
    "neutral": "white",
    "isolated": "dim",
    "tense": "yellow",
    "frustrated": "red",
    "stressed": "bold red",
}

_SUCCESS_COLORS: dict[str, str] = {
    "happy": "green",
    "content": "cyan",
    "neutral": "white",
    "frustrated": "red",
}

_SOCIAL_COLORS: dict[str, str] = {
    "social": "bright_cyan",
    "active": "green",
    "quiet": "white",
    "isolated": "dim",
}

_STRESS_COLORS: dict[str, str] = {
    "calm": "green",
    "relaxed": "cyan",
    "tense": "yellow",
    "stressed": "bold red",
}


def _print_mood(snap) -> None:
    """Render mood snapshot with Rich.

    Args:
        snap: :class:`~skcapstone.mood.MoodSnapshot` to display.
    """
    from rich.panel import Panel
    from rich.table import Table

    border_color = _SUMMARY_COLORS.get(snap.summary, "white")
    summary_color = _SUMMARY_COLORS.get(snap.summary, "white")

    header = f"[{summary_color}]{snap.summary.upper()}[/]"
    if snap.updated_at:
        header += f"  [dim]{snap.updated_at[:19]}Z[/]"

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("Dimension", style="bold", min_width=10)
    table.add_column("State", min_width=12)
    table.add_column("Detail", style="dim")

    # Success row
    sc = _SUCCESS_COLORS.get(snap.success_mood, "white")
    table.add_row(
        "Success",
        f"[{sc}]{snap.success_mood}[/]",
        f"{snap.responses_sent}/{snap.messages_processed} responses  ({snap.success_rate:.0%})",
    )

    # Social row
    soc = _SOCIAL_COLORS.get(snap.social_mood, "white")
    table.add_row(
        "Social",
        f"[{soc}]{snap.social_mood}[/]",
        f"{snap.messages_per_hour:.1f} msgs/hr  (window: {snap.window_hours}h)",
    )

    # Stress row
    stc = _STRESS_COLORS.get(snap.stress_mood, "white")
    table.add_row(
        "Stress",
        f"[{stc}]{snap.stress_mood}[/]",
        f"{snap.errors} errors  ({snap.error_rate:.0%} error rate)",
    )

    console.print()
    console.print(Panel(header, title="[bold]Agent Mood[/]", border_style=border_color))
    console.print(table)
    console.print()
