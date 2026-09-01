"""Mero observed-charter CLI."""

from __future__ import annotations

import json
from pathlib import Path

import click

from ._common import AGENT_HOME


def register_mero_commands(main: click.Group) -> None:
    """Register read-only Mero observation commands."""

    @main.group()
    def mero() -> None:
        """Observe what the estate is actually working on."""

    @mero.group()
    def charter() -> None:
        """Report title-prefix workstreams."""

    @charter.command("observe")
    @click.option("--home", default=AGENT_HOME, type=click.Path(path_type=Path))
    def charter_observe(home: Path) -> None:
        """Emit the read-only observed charter as JSON."""
        from ..card_store import CardStore
        from ..mero_charter import observe

        click.echo(json.dumps(observe(CardStore(home.expanduser())), indent=2, sort_keys=True))
