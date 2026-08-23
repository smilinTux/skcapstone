"""Tests for the ``coord describe`` CLI verb (SPE P3.1, card be2e849a).

``describe`` is the operator-facing half of the folded title/description: it
appends one enveloped event to both sanctioned append-only paths (the kanban
overlay and, when mirroring is enabled, the card's own store log). It never
touches the write-once ``core.json``, so the edit is attributable and
reversible rather than destructive.
"""

from pathlib import Path

import click
from click.testing import CliRunner

from skcapstone.card import CardEventLog, KanbanBoard
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


def test_describe_updates_the_folded_description(tmp_path: Path):
    _seed(tmp_path, "aaa11111", "Card", "a very long original description")
    result = CliRunner().invoke(
        _main(),
        [
            "coord",
            "describe",
            "aaa11111",
            "--home",
            str(tmp_path),
            "--description",
            "tightened",
            "--agent",
            "lumina",
        ],
    )
    assert result.exit_code == 0, result.output
    card = next(c for c in KanbanBoard(tmp_path).cards() if c.id == "aaa11111")
    assert card.description == "tightened"
    assert card.title == "Card"


def test_describe_updates_the_folded_title(tmp_path: Path):
    _seed(tmp_path, "bbb22222", "typo in titel", "body")
    result = CliRunner().invoke(
        _main(),
        [
            "coord",
            "describe",
            "bbb22222",
            "--home",
            str(tmp_path),
            "--title",
            "typo in title",
            "--agent",
            "lumina",
        ],
    )
    assert result.exit_code == 0, result.output
    card = next(c for c in KanbanBoard(tmp_path).cards() if c.id == "bbb22222")
    assert card.title == "typo in title"
    assert card.description == "body"


def test_describe_leaves_core_json_write_once(tmp_path: Path):
    _seed(tmp_path, "ccc33333", "Card", "original")
    core_path = CardStore(tmp_path).cards_dir / "ccc33333" / "core.json"
    before = core_path.read_text(encoding="utf-8")
    CliRunner().invoke(
        _main(),
        [
            "coord",
            "describe",
            "ccc33333",
            "--home",
            str(tmp_path),
            "--title",
            "New",
            "--description",
            "New body",
            "--agent",
            "lumina",
        ],
    )
    assert core_path.read_text(encoding="utf-8") == before


def test_describe_writes_an_attributed_overlay_event(tmp_path: Path):
    _seed(tmp_path, "ddd44444", "Card", "original")
    CliRunner().invoke(
        _main(),
        [
            "coord",
            "describe",
            "ddd44444",
            "--home",
            str(tmp_path),
            "--description",
            "edited",
            "--agent",
            "lumina",
        ],
    )
    events = [e for e in CardEventLog(tmp_path).read_all() if e.card_id == "ddd44444"]
    assert len(events) == 1
    assert events[0].action == "describe"
    assert events[0].writer == "lumina"
    assert events[0].description == "edited"
    assert events[0].title is None  # untouched field is not written


def test_describe_requires_a_field_to_change(tmp_path: Path):
    _seed(tmp_path, "eee55555", "Card", "original")
    result = CliRunner().invoke(
        _main(),
        ["coord", "describe", "eee55555", "--home", str(tmp_path), "--agent", "lumina"],
    )
    assert result.exit_code != 0
    assert not CardEventLog(tmp_path).read_all()
