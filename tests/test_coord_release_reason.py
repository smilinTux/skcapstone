"""release-claim accepts an optional reason and passes it through.

The task brief's literal snippet (`from skcapstone.cli.coord import coord`)
does not match this codebase: `coord` is a nested group built by
`register_coord_commands(main)`, not a module-level name (see
tests/test_cli_coord_deps.py for the established `_main()` pattern this
mirrors).
"""

from __future__ import annotations

import click
from click.testing import CliRunner

from skcapstone.cli.coord import register_coord_commands


def _main() -> click.Group:
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def test_release_claim_accepts_an_abandon_reason():
    result = CliRunner().invoke(_main(), ["coord", "release-claim", "--help"])
    assert result.exit_code == 0
    assert "--abandon-reason" in result.output


def test_abandon_reason_is_optional():
    """Omitting the flag must stay valid; the worker trap does not pass one."""
    result = CliRunner().invoke(_main(), ["coord", "release-claim", "--help"])
    assert "[required]" not in result.output.split("--abandon-reason")[1][:120]
