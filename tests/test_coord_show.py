"""Tests for ``coord show``, the read verb for a single card.

The board had no way to read one card. Every other read was board-wide
(``status``, ``board``, ``kanban``), so anyone wanting one card reached for
``coord describe``, whose name reads like a query in every other CLI on the
estate and which actually appends an edit event. A help audit over all 388
reachable commands found this was the only read-sounding name performing a
write, so the fix is a real read verb plus a describe error that names it.
"""

from __future__ import annotations

import json

import click
from click.testing import CliRunner

from skcapstone.card_store import CardCore, CardStore
from skcapstone.cli.coord import register_coord_commands
from skcapstone.coordination import Board, Task, TaskPriority


def _main() -> click.Group:
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _seed(tmp_path, task_id: str, title: str = "Card", labels=()) -> None:
    """Seed the way ``coord create`` really does it.

    Labels reach a card as ``Task.tags``, which ``mirror_coord_create`` copies
    into ``CardCore.initial_labels``. Passing ``initial_labels`` straight to
    ``CardStore.create`` after the board hook has already written core.json is
    a silent no-op, so a test seeded that way asserts against empty labels and
    then blames the reader for what is really a fixture bug.
    """
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(
        Task(id=task_id, title=title, priority=TaskPriority("high"), tags=list(labels))
    )
    if CardStore(tmp_path).fold(task_id) is None:  # board hook did not mirror
        CardStore(tmp_path).create(
            CardCore(id=task_id, title=title, initial_labels=list(labels))
        )


def _core_text(tmp_path, task_id: str) -> str:
    return (CardStore(tmp_path).cards_dir / task_id / "core.json").read_text(encoding="utf-8")


def test_show_renders_the_folded_card(tmp_path):
    _seed(tmp_path, "aaaa1111", title="[SKGW-01][M] A real title", labels=["seat-link"])
    res = CliRunner().invoke(_main(), ["coord", "show", "aaaa1111", "--home", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "aaaa1111" in res.output
    assert "A real title" in res.output
    assert "seat-link" in res.output


def test_show_json_is_parseable_and_carries_the_id(tmp_path):
    _seed(tmp_path, "bbbb2222", title="JSON card")
    res = CliRunner().invoke(
        _main(), ["coord", "show", "bbbb2222", "--home", str(tmp_path), "--json"]
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["id"] == "bbbb2222"
    assert data["title"] == "JSON card"


def test_show_is_read_only(tmp_path):
    """The read verb must not mutate the card it reads."""
    _seed(tmp_path, "cccc3333")
    before = _core_text(tmp_path, "cccc3333")
    events = CardStore(tmp_path).cards_dir / "cccc3333" / "events"
    before_events = sorted(p.name for p in events.glob("*")) if events.exists() else []

    res = CliRunner().invoke(_main(), ["coord", "show", "cccc3333", "--home", str(tmp_path)])
    assert res.exit_code == 0, res.output

    assert _core_text(tmp_path, "cccc3333") == before
    after_events = sorted(p.name for p in events.glob("*")) if events.exists() else []
    assert after_events == before_events


def test_show_on_a_missing_card_points_at_the_board_view(tmp_path):
    res = CliRunner().invoke(_main(), ["coord", "show", "deadbeef", "--home", str(tmp_path)])
    assert res.exit_code != 0
    assert "deadbeef" in res.output
    assert "coord status" in res.output


def test_describe_without_options_names_the_read_verb(tmp_path):
    """The original error said only 'Pass --title and/or --description'.

    That tells a caller who wanted to READ the card nothing about where to go,
    which is exactly how the confusion was reached in the first place.
    """
    _seed(tmp_path, "dddd4444")
    res = CliRunner().invoke(
        _main(), ["coord", "describe", "dddd4444", "--home", str(tmp_path)]
    )
    assert res.exit_code != 0
    assert "EDITS" in res.output
    assert "coord show dddd4444" in res.output


def test_both_verbs_document_each_other(tmp_path):
    show = CliRunner().invoke(_main(), ["coord", "show", "--help"])
    describe = CliRunner().invoke(_main(), ["coord", "describe", "--help"])
    assert show.exit_code == 0 and describe.exit_code == 0
    assert "coord describe" in show.output
    assert "coord show" in describe.output
    # both carry worked examples, which 302 of 388 commands still do not
    assert "Examples:" in show.output
    assert "Examples:" in describe.output
