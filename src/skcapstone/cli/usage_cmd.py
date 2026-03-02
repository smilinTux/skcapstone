"""Usage command: show daily/weekly/monthly token usage with cost estimates."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import click

from ._common import AGENT_HOME, console


def register_usage_commands(main: click.Group) -> None:
    """Register the ``skcapstone usage`` command group."""

    @main.group("usage")
    def usage_group():
        """Show LLM token usage and cost estimates.

        Tracks input/output tokens per model per day.
        Data is stored in ~/.skcapstone/usage/tokens-{date}.json.
        """

    @usage_group.command("daily")
    @click.option("--home", default=AGENT_HOME, type=click.Path(), help="Agent home directory.")
    @click.option("--date", "date_str", default=None, help="Date to show (YYYY-MM-DD, default: today).")
    @click.option("--model", default=None, help="Filter to a specific model.")
    @click.option("--json-out", is_flag=True, help="Output raw JSON.")
    def daily_cmd(home: str, date_str: str | None, model: str | None, json_out: bool):
        """Show token usage for a single day (default: today)."""
        from ..usage import UsageTracker
        tracker = UsageTracker(Path(home).expanduser())
        report = tracker.get_daily(date_str)
        if json_out:
            click.echo(json.dumps(_report_to_dict(report, model), indent=2))
            return
        _print_report(report, model, title_suffix=f"[dim]({report.date})[/]")

    @usage_group.command("weekly")
    @click.option("--home", default=AGENT_HOME, type=click.Path(), help="Agent home directory.")
    @click.option("--model", default=None, help="Filter to a specific model.")
    @click.option("--json-out", is_flag=True, help="Output raw JSON.")
    @click.option("--aggregate", "do_aggregate", is_flag=True, default=True,
                  help="Show aggregated totals (default) instead of per-day table.")
    @click.option("--per-day", "do_aggregate", flag_value=False,
                  help="Show per-day breakdown instead of aggregated totals.")
    def weekly_cmd(home: str, model: str | None, json_out: bool, do_aggregate: bool):
        """Show token usage for the last 7 days."""
        from ..usage import UsageTracker
        tracker = UsageTracker(Path(home).expanduser())
        reports = tracker.get_weekly()
        _handle_range(tracker, reports, model, json_out, do_aggregate, "Last 7 days")

    @usage_group.command("monthly")
    @click.option("--home", default=AGENT_HOME, type=click.Path(), help="Agent home directory.")
    @click.option("--model", default=None, help="Filter to a specific model.")
    @click.option("--json-out", is_flag=True, help="Output raw JSON.")
    @click.option("--aggregate", "do_aggregate", is_flag=True, default=True,
                  help="Show aggregated totals (default).")
    @click.option("--per-day", "do_aggregate", flag_value=False,
                  help="Show per-day breakdown.")
    def monthly_cmd(home: str, model: str | None, json_out: bool, do_aggregate: bool):
        """Show token usage for the last 30 days."""
        from ..usage import UsageTracker
        tracker = UsageTracker(Path(home).expanduser())
        reports = tracker.get_monthly()
        _handle_range(tracker, reports, model, json_out, do_aggregate, "Last 30 days")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _handle_range(tracker, reports, model, json_out, do_aggregate, label):
    """Common handler for weekly/monthly range commands."""
    if do_aggregate:
        agg = tracker.aggregate(reports)
        if json_out:
            click.echo(json.dumps(_report_to_dict(agg, model), indent=2))
            return
        _print_report(agg, model, title_suffix=f"[dim]({label})[/]")
    else:
        if json_out:
            click.echo(json.dumps(
                [_report_to_dict(r, model) for r in reports], indent=2
            ))
            return
        _print_per_day_table(reports, model, label)


def _report_to_dict(report, model_filter: str | None) -> dict:
    """Serialize a DailyUsageReport to a plain dict, optionally filtered."""
    models = report.models
    if model_filter:
        models = {k: v for k, v in models.items() if model_filter.lower() in k.lower()}
    return {
        "date": report.date,
        "total_calls": sum(m.calls for m in models.values()),
        "total_input_tokens": sum(m.input_tokens for m in models.values()),
        "total_output_tokens": sum(m.output_tokens for m in models.values()),
        "total_cost_usd": round(sum(m.estimated_cost_usd for m in models.values()), 6),
        "models": {
            name: {
                "calls": m.calls,
                "input_tokens": m.input_tokens,
                "output_tokens": m.output_tokens,
                "estimated_cost_usd": m.estimated_cost_usd,
            }
            for name, m in sorted(models.items())
        },
    }


def _print_report(report, model_filter: str | None, title_suffix: str = "") -> None:
    """Render a usage report with Rich."""
    from rich.panel import Panel
    from rich.table import Table

    models = dict(report.models)
    if model_filter:
        models = {k: v for k, v in models.items() if model_filter.lower() in k.lower()}

    total_calls = sum(m.calls for m in models.values())
    total_inp = sum(m.input_tokens for m in models.values())
    total_out = sum(m.output_tokens for m in models.values())
    total_cost = sum(m.estimated_cost_usd for m in models.values())

    summary = [
        f"[bold]Date:[/]          {report.date}",
        f"[bold]Total calls:[/]   {total_calls:,}",
        f"[bold]Input tokens:[/]  {total_inp:,}",
        f"[bold]Output tokens:[/] {total_out:,}",
        f"[bold]Total tokens:[/]  {total_inp + total_out:,}",
        f"[bold]Est. cost:[/]     [{'green' if total_cost < 0.01 else 'yellow'}]${total_cost:.4f} USD[/]",
    ]

    console.print()
    console.print(Panel(
        "\n".join(summary),
        title=f"[cyan]Token Usage[/] {title_suffix}",
        border_style="cyan",
    ))

    if not models:
        console.print("[dim]No usage data for this period.[/]")
        console.print()
        return

    table = Table(title="Per-model breakdown", box=None, padding=(0, 2))
    table.add_column("Model", style="bold")
    table.add_column("Calls", justify="right")
    table.add_column("Input", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Est. Cost", justify="right", style="yellow")

    for name, m in sorted(models.items(), key=lambda x: -x[1].total_tokens):
        cost_str = f"${m.estimated_cost_usd:.4f}" if m.estimated_cost_usd >= 0.0001 else "free"
        table.add_row(
            name,
            f"{m.calls:,}",
            f"{m.input_tokens:,}",
            f"{m.output_tokens:,}",
            f"{m.total_tokens:,}",
            cost_str,
        )

    console.print(table)
    console.print()


def _print_per_day_table(reports, model_filter: str | None, label: str) -> None:
    """Render a per-day breakdown table."""
    from rich.table import Table

    table = Table(title=f"Per-day usage — {label}", box=None, padding=(0, 2))
    table.add_column("Date", style="bold")
    table.add_column("Calls", justify="right")
    table.add_column("Input", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Est. Cost", justify="right", style="yellow")

    for r in reports:
        models = dict(r.models)
        if model_filter:
            models = {k: v for k, v in models.items() if model_filter.lower() in k.lower()}
        calls = sum(m.calls for m in models.values())
        inp = sum(m.input_tokens for m in models.values())
        out = sum(m.output_tokens for m in models.values())
        cost = sum(m.estimated_cost_usd for m in models.values())
        cost_str = f"${cost:.4f}" if cost >= 0.0001 else "free"
        row_style = "" if calls > 0 else "dim"
        table.add_row(r.date, f"{calls:,}", f"{inp:,}", f"{out:,}", cost_str, style=row_style)

    console.print()
    console.print(table)
    console.print()
