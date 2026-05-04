"""Shell command — launch the interactive sovereign agent REPL."""

from __future__ import annotations

import sys
from importlib.resources import as_file, files
from pathlib import Path

import click

from ._common import AGENT_HOME


def _picker_path() -> Path:
    """Resolve the absolute path to the bundled sk-agent-picker.sh.

    Works for any install layout (PyPI wheel, editable, install.sh) by
    going through importlib.resources, so the picker is always sourced
    from inside the installed skcapstone package.
    """
    resource = files("skcapstone") / "data" / "sk-agent-picker.sh"
    with as_file(resource) as p:
        return Path(p)


def register_shell_commands(main: click.Group) -> None:
    """Register the 'shell', 'shell-init', and 'shell-picker-path' commands."""

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

    @main.command("shell-init")
    def shell_init_cmd() -> None:
        """Emit shell code that loads the SK agent picker.

        Add to your ~/.bashrc (or ~/.zshrc):

            eval "$(skcapstone shell-init)"

        This sources the picker shipped inside the skcapstone package,
        so a single `pip install skcapstone` (PyPI, editable, or via
        install.sh) is enough — no external script copy required.
        """
        path = _picker_path()
        if not path.is_file():
            click.echo(f"# skcapstone: picker missing at {path}", err=True)
            sys.exit(1)
        click.echo(f'source "{path}"')

    @main.command("shell-picker-path")
    def shell_picker_path_cmd() -> None:
        """Print the absolute path to the bundled sk-agent-picker.sh.

        Useful for hand-rolled shell wiring, debugging, or scripted
        invocation of the picker.
        """
        path = _picker_path()
        click.echo(str(path))
        if not path.is_file():
            sys.exit(1)
