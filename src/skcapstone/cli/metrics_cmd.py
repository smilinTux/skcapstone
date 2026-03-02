"""Metrics command: show today's consciousness loop stats."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import click

from ._common import AGENT_HOME, console


def register_metrics_commands(main: click.Group) -> None:
    """Register the ``skcapstone metrics`` command."""

    @main.command("metrics")
    @click.option("--home", default=AGENT_HOME, type=click.Path(), help="Agent home directory.")
    @click.option("--port", default=7777, help="Daemon API port (default: 7777).")
    @click.option("--json-out", is_flag=True, help="Output raw JSON.")
    @click.option("--date", "date_str", default=None, help="Show metrics for a specific date (YYYY-MM-DD).")
    def metrics_cmd(home: str, port: int, json_out: bool, date_str: str | None):
        """Show today's consciousness loop metrics.

        Tries the running daemon first (GET /api/v1/metrics).
        Falls back to reading the persisted daily JSON file.
        """
        data = _fetch_from_daemon(port)
        if data is None:
            data = _read_from_file(Path(home).expanduser(), date_str)

        if data is None:
            if json_out:
                click.echo(json.dumps({"error": "No metrics available"}))
            else:
                console.print("[yellow]No metrics found.[/] Start the daemon or run a test message first.")
            return

        if json_out:
            click.echo(json.dumps(data, indent=2))
            return

        _print_metrics(data)


def _fetch_from_daemon(port: int) -> dict | None:
    """Try GET /api/v1/metrics from the running daemon."""
    import urllib.request
    import urllib.error

    try:
        url = f"http://127.0.0.1:{port}/api/v1/metrics"
        with urllib.request.urlopen(url, timeout=2) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _read_from_file(home: Path, date_str: str | None) -> dict | None:
    """Read a daily metrics JSON from disk."""
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = home / "metrics" / "daily" / f"{date_str}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _print_metrics(data: dict) -> None:
    """Render metrics in a rich panel."""
    from rich.panel import Panel
    from rich.table import Table

    date = data.get("date", "?")
    msgs = data.get("messages_processed", 0)
    responses = data.get("responses_sent", 0)
    errors = data.get("errors", 0)
    rt = data.get("response_time_ms", {})

    error_color = "red" if errors > 0 else "green"
    rt_str = (
        f"min={rt.get('min', 0):.0f}ms  "
        f"avg={rt.get('avg', 0):.0f}ms  "
        f"p99={rt.get('p99', 0):.0f}ms  "
        f"max={rt.get('max', 0):.0f}ms  "
        f"n={rt.get('count', 0)}"
    ) if rt.get("count", 0) > 0 else "[dim]no data[/]"

    summary_lines = [
        f"[bold]Date:[/]          {date}",
        f"[bold]Messages:[/]      {msgs}",
        f"[bold]Responses:[/]     {responses}",
        f"[bold]Errors:[/]        [{error_color}]{errors}[/]",
        f"[bold]Response time:[/] {rt_str}",
    ]
    console.print()
    console.print(Panel(
        "\n".join(summary_lines),
        title="[cyan]Consciousness Metrics[/]",
        border_style="cyan",
    ))

    # Backend usage table
    backend_usage = data.get("backend_usage", {})
    if backend_usage:
        table = Table(title="Backend Usage", box=None, padding=(0, 2))
        table.add_column("Backend", style="bold")
        table.add_column("Requests", justify="right")
        for bk, count in sorted(backend_usage.items(), key=lambda x: -x[1]):
            table.add_row(bk, str(count))
        console.print(table)

    # Tier usage table
    tier_usage = data.get("tier_usage", {})
    if tier_usage:
        table = Table(title="Tier Usage", box=None, padding=(0, 2))
        table.add_column("Tier", style="bold")
        table.add_column("Requests", justify="right")
        for tier, count in sorted(tier_usage.items(), key=lambda x: -x[1]):
            table.add_row(tier, str(count))
        console.print(table)

    # Per-peer message counts (top 10)
    peer_counts = data.get("messages_per_peer", {})
    if peer_counts:
        table = Table(title="Messages per Peer (top 10)", box=None, padding=(0, 2))
        table.add_column("Peer", style="bold cyan")
        table.add_column("Messages", justify="right")
        for peer, count in sorted(peer_counts.items(), key=lambda x: -x[1])[:10]:
            table.add_row(peer, str(count))
        console.print(table)

    console.print()
