"""Autopilot cost overview: read-only summary over the skharness agent-run
execute bridge's per-run cost ledger (today / 7d / 30d / all-time / by-repo).

The ledger and its aggregation live in skharness (``skharness.autocode
.autopilot_cost``); this command is a thin, lazily-imported presentation
layer so a skcapstone install with no skharness sibling still works (it just
reports the tracker as unavailable rather than failing).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import click

from ._common import console


def register_autopilot_cost_commands(main: click.Group) -> None:
    """Register the ``skcapstone autopilot-cost`` command."""

    @main.command("autopilot-cost")
    @click.option("--json-out", is_flag=True, help="Output raw JSON.")
    def autopilot_cost_cmd(json_out: bool):
        """Show the autopilot agent-run bridge cost overview (today's spend
        vs the daily cap, last 7/30 days, all-time, and per-repo)."""
        try:
            from skharness.autocode.autopilot_cost import summary
        except ImportError:
            console.print("[dim]cost tracking unavailable (skharness not installed)[/]")
            return

        cap_usd = None
        try:
            from skharness.autocode.config import Config

            cap_usd = Config.load().caps.max_usd_per_day
        except Exception:  # noqa: BLE001 -- cap display is best-effort only
            cap_usd = None

        today = datetime.now(timezone.utc).date().isoformat()
        data = summary(today=today, cap_usd=cap_usd)

        if json_out:
            click.echo(json.dumps(data, indent=2, default=str))
            return

        _print_overview(data, today)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _row(label: str, agg: dict) -> str:
    # Joules lead (the canonical SKWorld cost unit); USD follows in parens.
    return (
        f"[bold]{label}:[/]".ljust(24)
        + f"{agg['joules']:,} J  (${agg['cost_usd']:.2f})  "
        + f"·  {agg['tokens']:,} tokens  ·  {agg['runs']} runs"
    )


def _print_overview(data: dict, today: str) -> None:
    from rich.panel import Panel
    from rich.table import Table

    cap_usd = data.get("cap_usd")
    cap_joules = data.get("cap_joules")
    pct = data.get("today_pct_of_cap")
    today_agg = data["today"]

    lines = [_row(f"Today ({today})", today_agg)]
    if cap_usd is not None:
        cap_line = "[bold]Daily cap:[/]".ljust(24) + f"{cap_joules:,} J  (${cap_usd:.2f})"
        if pct is not None:
            pct_color = "red" if pct >= 100 else ("yellow" if pct >= 80 else "green")
            cap_line += f"  [{pct_color}]({pct:.0f}% used)[/]"
        lines.append(cap_line)
    lines.append("")
    lines.append(_row("Last 7 days", data["last_7_days"]))
    lines.append(_row("Last 30 days", data["last_30_days"]))
    lines.append(_row("All time", data["all_time"]))

    console.print()
    console.print(
        Panel("\n".join(lines), title="[cyan]Autopilot Cost Overview[/]", border_style="cyan")
    )

    by_repo = data.get("by_repo") or {}
    if not by_repo:
        console.print("[dim]No runs recorded yet.[/]")
        console.print()
        return

    table = Table(title="By repo (all time)", box=None, padding=(0, 2))
    table.add_column("Repo", style="bold")
    table.add_column("Joules", justify="right", style="cyan")
    table.add_column("Cost", justify="right", style="yellow")
    table.add_column("Tokens", justify="right")
    table.add_column("Runs", justify="right")

    for repo, agg in sorted(by_repo.items(), key=lambda kv: -kv[1]["joules"]):
        table.add_row(
            repo,
            f"{agg['joules']:,}",
            f"${agg['cost_usd']:.4f}",
            f"{agg['tokens']:,}",
            str(agg["runs"]),
        )

    console.print(table)
    console.print()
