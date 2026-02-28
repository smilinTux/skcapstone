"""Shell command — launch the interactive sovereign agent REPL."""

from __future__ import annotations

import click

from ._common import AGENT_HOME


def register_shell_commands(main: click.Group) -> None:
    """Register the 'shell' command on the main CLI group."""

    @main.command("shell")
    @click.option(
        "--home",
        default=AGENT_HOME,
        help="Agent home directory.",
        type=click.Path(),
        show_default=True,
    )
    def shell_cmd(home: str) -> None:
        """Launch the interactive sovereign agent shell.

        An IPython-style REPL that exposes all agent operations:
        memory, chat, sync, coord, trust, soul, journal, and more.

        Uses prompt_toolkit when available (multi-level tab completion,
        persistent history, coloured prompt). Falls back to readline.

        \b
        Quick reference:
          status               Agent pillar overview
          memory search <q>    Search memories
          coord status         Show coordination board
          chat inbox           Check incoming messages
          sync push/pull       Synchronise with peers
          trust graph          Visualise the trust web
          help                 Full command reference
          exit                 Leave the shell
        """
        from ..shell import run_shell

        run_shell(home=home)
