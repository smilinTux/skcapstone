"""Tests for ``coord show`` and the ``describe`` -> ``edit`` rename.

``show`` is the single-card read that the coord CLI was missing: before it, the
only way to read one card was to render the whole board and filter it, which on
a multi-thousand-card store means megabytes of JSON to answer a question about
one id.

``describe`` is kept as a deprecated alias because every other CLI spells this
verb as a read (kubectl, aws, docker) while here it writes, so scripts that
already call it must keep working while being told to move.
"""

import json
from pathlib import Path

import click
from click.testing import CliRunner

from skcapstone.card_store import CardCore, CardStore
from skcapstone.cli.coord import register_coord_commands
from skcapstone.coordination import Board, Task


def _main() -> click.Group:
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _seed(tmp_path: Path, task_id: str, title: str, description: str) -> None:
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id=task_id, title=title, description=description))
    CardStore(tmp_path).create(CardCore(id=task_id, title=title, description=description))


def test_show_renders_the_card(tmp_path: Path):
    _seed(tmp_path, "aaa11111", "Renew the cert", "body text")
    result = CliRunner().invoke(_main(), ["coord", "show", "aaa11111", "--home", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "aaa11111" in result.output
    assert "Renew the cert" in result.output


def test_show_json_is_parseable_and_has_no_enum_reprs(tmp_path: Path):
    _seed(tmp_path, "bbb22222", "JSON card", "body")
    result = CliRunner().invoke(
        _main(), ["coord", "show", "bbb22222", "--home", str(tmp_path), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["id"] == "bbb22222"
    # mode="json" must resolve enums; "Kind.TASK" leaking through is the bug.
    assert "Kind." not in result.output
    assert "Column." not in result.output


def test_show_reports_a_missing_card_cleanly(tmp_path: Path):
    result = CliRunner().invoke(_main(), ["coord", "show", "nope0000", "--home", str(tmp_path)])
    assert result.exit_code != 0
    assert "No card nope0000" in result.output


def test_edit_writes_the_title(tmp_path: Path):
    _seed(tmp_path, "ccc33333", "Old title", "body")
    result = CliRunner().invoke(
        _main(),
        ["coord", "edit", "ccc33333", "--title", "New title", "--home", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert CardStore(tmp_path).fold("ccc33333").title == "New title"
    assert CardStore(tmp_path)._read_events("ccc33333")[-1]["writer"] == "coord-edit"


def test_describe_alias_warns_but_still_writes(tmp_path: Path):
    """The alias must keep working: a warning that silently dropped the write
    would be worse than the naming collision it is warning about."""
    _seed(tmp_path, "ddd44444", "Old title", "body")
    result = CliRunner().invoke(
        _main(),
        ["coord", "describe", "ddd44444", "--title", "Aliased title", "--home", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "deprecated" in result.output
    assert CardStore(tmp_path).fold("ddd44444").title == "Aliased title"
    assert CardStore(tmp_path)._read_events("ddd44444")[-1]["writer"] == "coord-edit"


def test_describe_alias_still_rejects_an_empty_edit(tmp_path: Path):
    _seed(tmp_path, "eee55555", "Old title", "body")
    result = CliRunner().invoke(
        _main(), ["coord", "describe", "eee55555", "--home", str(tmp_path)]
    )
    assert result.exit_code != 0
    assert CardStore(tmp_path).fold("eee55555").title == "Old title"
