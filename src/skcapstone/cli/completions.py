"""Shell completions commands: install, show, uninstall."""

from __future__ import annotations

import sys

import click

from ._common import console


def register_completions_commands(main: click.Group) -> None:
    """Register the completions command group."""

    @main.group()
    def completions():
        """Shell tab completion — sovereign autocomplete.

        Install, show, or remove tab completion scripts for
        bash, zsh, and fish.
        """

    @completions.command("install")
    @click.option("--shell", "shell_name", default=None, type=click.Choice(["bash", "zsh", "fish"]))
    def completions_install(shell_name):
        """Install tab completion for your shell."""
        from ..completions import install_completions

        result = install_completions(shell=shell_name)

        if not result.get("success"):
            console.print(f"\n  [red]{result.get('error', 'Install failed')}[/]\n")
            sys.exit(1)

        console.print(f"\n  [green]Tab completion installed for {result['shell']}[/]")
        console.print(f"  Script: {result['script_path']}")
        if result.get("rc_updated"):
            console.print(f"  RC updated: {result.get('rc_path')}")
        console.print(f"  [dim]Restart your shell or run: source {result['script_path']}[/]\n")

    @completions.command("show")
    @click.option("--shell", "shell_name", default=None, type=click.Choice(["bash", "zsh", "fish"]))
    def completions_show(shell_name):
        """Print the completion script to stdout."""
        from ..completions import detect_shell, generate_script

        shell = shell_name or detect_shell() or "bash"
        click.echo(generate_script(shell))

    @completions.command("uninstall")
    @click.option("--shell", "shell_name", default=None, type=click.Choice(["bash", "zsh", "fish"]))
    def completions_uninstall(shell_name):
        """Remove installed completion scripts."""
        from ..completions import uninstall_completions

        result = uninstall_completions(shell=shell_name)

        if result["removed"]:
            for path in result["removed"]:
                console.print(f"  [green]Removed:[/] {path}")
        else:
            console.print("  [dim]No completion scripts found to remove.[/]")
        console.print(f"  [dim]{result['note']}[/]")
        console.print()
