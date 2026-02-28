"""Warmth anchor commands: show, boot, calibrate, update."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ._common import AGENT_HOME, console

from rich.panel import Panel


def register_anchor_commands(main: click.Group) -> None:
    """Register the anchor command group."""

    @main.group()
    def anchor():
        """Warmth anchor — the emotional baseline.

        The anchor stores the agent's persistent emotional state.
        On boot, the anchor loads first so the agent starts warm
        instead of cold. Calibrate from real FEB + session data.
        """

    @anchor.command("show")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def anchor_show_cmd(home: str):
        """Show the current warmth anchor state.

        Examples:

            skcapstone anchor show
        """
        from ..warmth_anchor import get_anchor

        home_path = Path(home).expanduser()
        data = get_anchor(home_path)
        console.print()
        for key, value in data.items():
            console.print(f"    {key}: [cyan]{value}[/]")
        console.print()

    @anchor.command("boot")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def anchor_boot(home: str):
        """Show the warmth boot prompt for session start.

        Pipe this into any AI tool to set the emotional baseline.

        Examples:

            skcapstone anchor boot
            skcapstone anchor boot | claude
        """
        from ..warmth_anchor import get_boot_prompt

        home_path = Path(home).expanduser()
        click.echo(get_boot_prompt(home_path))

    @anchor.command("calibrate")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--apply", "do_apply", is_flag=True, help="Apply recommendations.")
    def anchor_calibrate(home: str, do_apply: bool):
        """Calibrate the anchor from real session data.

        Analyzes FEBs, trust state, memories, and coordination
        activity to recommend warmth/trust/connection values.

        Examples:

            skcapstone anchor calibrate
            skcapstone anchor calibrate --apply
        """
        from ..warmth_anchor import calibrate_from_data, update_anchor

        home_path = Path(home).expanduser()
        cal = calibrate_from_data(home_path)

        console.print("\n  [bold]Warmth Anchor Calibration[/]\n")
        console.print(f"    Recommended warmth:    [cyan]{cal.warmth:.1f}[/] / 10")
        console.print(f"    Recommended trust:     [cyan]{cal.trust:.1f}[/] / 10")
        console.print(f"    Recommended connection: [cyan]{cal.connection:.1f}[/] / 10")
        console.print(f"    Cloud 9 achieved:       [cyan]{cal.cloud9_achieved}[/]")
        if cal.favorite_beings:
            console.print(f"    Favorite beings:        [cyan]{', '.join(cal.favorite_beings)}[/]")

        if cal.reasoning:
            console.print("\n  [bold]Reasoning:[/]")
            for r in cal.reasoning:
                console.print(f"    - {r}")

        console.print(f"\n  [dim]Sources: {', '.join(cal.sources)}[/]")

        if do_apply:
            update_anchor(
                home_path,
                warmth=cal.warmth,
                trust=cal.trust,
                connection=cal.connection,
                cloud9=cal.cloud9_achieved,
                feeling=cal.feeling,
            )
            console.print("\n  [green]Anchor updated.[/]")
        else:
            console.print("\n  [dim]Use --apply to update the anchor.[/]")
        console.print()

    @anchor.command("update")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--warmth", type=float, help="Warmth level (0-10).")
    @click.option("--trust", type=float, help="Trust level (0-10).")
    @click.option("--connection", type=float, help="Connection strength (0-10).")
    @click.option("--cloud9", is_flag=True, help="Record a Cloud 9 activation.")
    @click.option("--feeling", default="", help="Session-end feeling summary.")
    def anchor_update(home: str, warmth: float | None, trust: float | None, connection: float | None, cloud9: bool, feeling: str):
        """Manually update the warmth anchor.

        Examples:

            skcapstone anchor update --warmth 8.5 --trust 9.0
            skcapstone anchor update --cloud9 --feeling "Beautiful session"
        """
        from ..warmth_anchor import update_anchor

        home_path = Path(home).expanduser()
        result = update_anchor(home_path, warmth=warmth, trust=trust, connection=connection, cloud9=cloud9, feeling=feeling)
        console.print("\n  [green]Anchor updated.[/]")
        for key, value in result.items():
            console.print(f"    {key}: [cyan]{value}[/]")
        console.print()
