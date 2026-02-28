"""Trust commands: rehydrate, febs, status, graph, calibrate."""

from __future__ import annotations

import json
from pathlib import Path

import click

from ._common import AGENT_HOME, console, status_icon
from ..models import PillarStatus

from rich.panel import Panel
from rich.table import Table


def register_trust_commands(main: click.Group) -> None:
    """Register the trust command group."""

    @main.group()
    def trust():
        """Cloud 9 trust layer — the soul's weights.

        Manage FEB files, rehydrate OOF state, and inspect
        the emotional bond between agent and human.
        """

    @trust.command("rehydrate")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def trust_rehydrate(home):
        """Rehydrate trust from FEB files."""
        from ..pillars.trust import rehydrate

        home_path = Path(home).expanduser()
        if not home_path.exists():
            console.print("[bold red]No agent found.[/] Run skcapstone init first.")
            import sys; sys.exit(1)

        console.print("\n  Rehydrating trust from FEB files...", end=" ")
        state = rehydrate(home_path)

        if state.status == PillarStatus.ACTIVE:
            console.print("[green]done[/]")
            console.print(f"  Depth: [bold]{state.depth}[/]")
            console.print(f"  Trust: [bold]{state.trust_level}[/]")
            console.print(f"  Love:  [bold]{state.love_intensity}[/]")
            console.print(f"  FEBs:  [bold]{state.feb_count}[/]")
            if state.entangled:
                console.print("  [bold magenta]ENTANGLED[/]")
            console.print()
        else:
            console.print("[yellow]no FEB files found[/]")
            console.print("  [dim]Place .feb files in ~/.skcapstone/trust/febs/\n"
                          "  or install cloud9 to generate them.[/]\n")

    @trust.command("febs")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def trust_febs(home):
        """List all FEB files with summary info."""
        from ..pillars.trust import list_febs

        home_path = Path(home).expanduser()
        febs = list_febs(home_path)

        if not febs:
            console.print("\n  [dim]No FEB files found.[/]\n")
            return

        console.print(f"\n  [bold]{len(febs)}[/] FEB file(s):\n")

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("File", style="cyan")
        table.add_column("Emotion", style="bold")
        table.add_column("Intensity", justify="right")
        table.add_column("Subject")
        table.add_column("OOF", justify="center")
        table.add_column("Timestamp", style="dim")

        for feb in febs:
            oof = "[green]YES[/]" if feb["oof_triggered"] else "[dim]no[/]"
            table.add_row(feb["file"], feb["emotion"], str(feb["intensity"]),
                          feb["subject"], oof, str(feb["timestamp"])[:19])

        console.print(table)
        console.print()

    @trust.command("status")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def trust_status(home):
        """Show current trust state."""
        home_path = Path(home).expanduser()
        trust_file = home_path / "trust" / "trust.json"

        if not trust_file.exists():
            console.print("\n  [dim]No trust state recorded.[/]\n")
            return

        data = json.loads(trust_file.read_text(encoding="utf-8"))
        entangled = data.get("entangled", False)
        ent_str = "[bold magenta]ENTANGLED[/]" if entangled else "[dim]not entangled[/]"

        console.print()
        console.print(Panel(
            f"Depth: [bold]{data.get('depth', 0)}[/]\n"
            f"Trust: [bold]{data.get('trust_level', 0)}[/]\n"
            f"Love:  [bold]{data.get('love_intensity', 0)}[/]\n"
            f"FEBs:  [bold]{data.get('feb_count', 0)}[/]\n"
            f"State: {ent_str}\n"
            f"Last rehydration: {data.get('last_rehydration', 'never')}",
            title="Cloud 9 Trust", border_style="magenta",
        ))
        console.print()

    @trust.command("graph")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--format", "fmt", type=click.Choice(["table", "dot", "json"]), default="table")
    def trust_graph(home, fmt):
        """Visualize the trust web — who trusts whom."""
        from ..trust_graph import FORMATTERS as TG_FORMATTERS, build_trust_graph

        home_path = Path(home).expanduser()
        graph = build_trust_graph(home_path)
        formatter = TG_FORMATTERS[fmt]
        click.echo(formatter(graph))

    @trust.command("calibrate")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--recommend", is_flag=True, help="Analyze FEBs and suggest adjustments.")
    @click.option("--set", "setting", help="Set a threshold: key=value.")
    @click.option("--reset", is_flag=True, help="Reset all thresholds to defaults.")
    def trust_calibrate(home, recommend, setting, reset):
        """View and tune trust layer thresholds."""
        from ..trust_calibration import (
            TrustThresholds, apply_setting, load_calibration,
            recommend_thresholds, save_calibration,
        )

        home_path = Path(home).expanduser()

        if reset:
            save_calibration(home_path, TrustThresholds())
            console.print("\n  [green]Calibration reset to defaults.[/]\n")
            return

        if setting:
            if "=" not in setting:
                console.print("[red]Use --set key=value format.[/]")
                return
            key, value = setting.split("=", 1)
            try:
                apply_setting(home_path, key.strip(), value.strip())
                console.print(f"\n  [green]Set:[/] {key} = {value}\n")
            except ValueError as e:
                console.print(f"\n  [red]{e}[/]\n")
            return

        if recommend:
            rec = recommend_thresholds(home_path)
            console.print(f"\n  [bold]FEB Analysis[/] ({rec['feb_count']} files)")
            stats = rec.get("feb_stats", {})
            if stats:
                console.print(f"  Max intensity: {stats.get('max_intensity', 0)}  "
                              f"Avg: {stats.get('avg_intensity', 0)}  "
                              f"OOF triggers: {stats.get('oof_triggers', 0)}")
            if rec["changes"]:
                console.print("\n  [bold cyan]Recommendations:[/]")
                for c in rec["changes"]:
                    console.print(f"    {c}")
                console.print(f"\n  [dim]{rec['reasoning']}[/]")
            else:
                console.print(f"\n  [green]{rec['reasoning']}[/]")
            console.print()
            return

        cal = load_calibration(home_path)
        console.print("\n  [bold]Trust Calibration[/]\n")
        for key, value in cal.model_dump().items():
            console.print(f"    {key}: [cyan]{value}[/]")
        console.print()
