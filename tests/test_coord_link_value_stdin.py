"""A link value over the OS argv limit is read from stdin."""

from __future__ import annotations

import json

from click.testing import CliRunner

from skcapstone.card_store import CardCore, CardStore
from skcapstone.cli.coord import register_coord_commands
from skcapstone.coordination import Board, Task


def _main():
    import click

    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _seed(home, card_id="deadbeef"):
    board = Board(home)
    board.ensure_dirs()
    board.create_task(Task(id=card_id, title="Card", description="body"))
    CardStore(home).create(CardCore(id=card_id, title="Card", description="body"))


def _link(home, value, *extra, stdin=None):
    return CliRunner().invoke(
        _main(),
        [
            "coord",
            "link",
            "deadbeef",
            "test_profile_recipe",
            value,
            *extra,
            "--agent",
            "lumina-nor",
            "--home",
            str(home),
        ],
        input=stdin,
    )


def test_value_over_the_argv_limit_links_from_stdin(tmp_path, monkeypatch):
    """Linux caps one argv string at 128 KiB; SKLegal baselines exceed it."""
    monkeypatch.setattr("skcapstone.jarvis_emergency.authorize_coord_mutation", lambda *a: None)
    _seed(tmp_path)
    recipe = json.dumps(
        {"pytest": {f"tests/test_{i}.py": 1 for i in range(9000)}, "deselect": []},
        sort_keys=True,
        separators=(",", ":"),
    )
    assert len(recipe) > 131072
    result = _link(tmp_path, "-", "--value-from-stdin", stdin=recipe)
    assert result.exit_code == 0, result.output
    assert CardStore(tmp_path).fold("deadbeef").links["test_profile_recipe"] == recipe


def test_stdin_flag_requires_dash_value(tmp_path, monkeypatch):
    monkeypatch.setattr("skcapstone.jarvis_emergency.authorize_coord_mutation", lambda *a: None)
    _seed(tmp_path)
    result = _link(tmp_path, "inline", "--value-from-stdin", stdin="ignored")
    assert result.exit_code != 0
    assert "requires VALUE to be -" in result.output
    assert "test_profile_recipe" not in (CardStore(tmp_path).fold("deadbeef").links or {})
