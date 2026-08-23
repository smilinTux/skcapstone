"""Tests for the ``coord rehome`` CLI verb and rehome module (card a680158b).

``rehome`` is a fold-native path-prefix rewrite: it appends one attributed
``describe`` event per affected card (the same write path as ``coord
describe``), so ``core.json`` stays write-once and a repository move becomes
one command instead of a sweep of hand edits. All tests run against FIXTURE
card directories under ``tmp_path`` - never the live board.
"""

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from skcapstone.card import CardEvent, CardEventLog, KanbanBoard
from skcapstone.card_store import CardCore, CardStore
from skcapstone.cli.coord import register_coord_commands
from skcapstone.coordination import Board, Task
from skcapstone.rehome import find_rehome_matches, rehome_descriptions

OLD = "/mnt/cloud/onedrive/projects/DAVE AI"
NEW = "/mnt/cloud/onedrive/projects/DAVE-AI"


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


def _folded_description(tmp_path: Path, task_id: str) -> str:
    card = next(c for c in KanbanBoard(tmp_path).cards() if c.id == task_id)
    return card.description


def test_rehome_rewrites_only_matching_descriptions(tmp_path: Path):
    _seed(tmp_path, "aaa11111", "Old", f"Fix {OLD}/sklegal/docs/spec.md now")
    _seed(tmp_path, "bbb22222", "Clean", "no paths here")
    report = rehome_descriptions(tmp_path, OLD, NEW, agent="lumina")
    assert report["matched"] == 1
    assert report["rewritten"] == 1
    assert report["cards"] == ["aaa11111"]
    assert _folded_description(tmp_path, "aaa11111") == f"Fix {NEW}/sklegal/docs/spec.md now"
    assert _folded_description(tmp_path, "bbb22222") == "no paths here"


def test_rehome_replaces_every_occurrence_in_a_description(tmp_path: Path):
    _seed(tmp_path, "ccc33333", "Twice", f"{OLD}/a and later {OLD}/b")
    rehome_descriptions(tmp_path, OLD, NEW)
    assert _folded_description(tmp_path, "ccc33333") == f"{NEW}/a and later {NEW}/b"


def test_rehome_is_idempotent_on_a_second_run(tmp_path: Path):
    _seed(tmp_path, "ddd44444", "Card", f"see {OLD}/x")
    first = rehome_descriptions(tmp_path, OLD, NEW)
    second = rehome_descriptions(tmp_path, OLD, NEW)
    assert first["matched"] == 1
    assert second["matched"] == 0
    assert second["rewritten"] == 0


def test_rehome_is_reversible_by_swapping_arguments(tmp_path: Path):
    _seed(tmp_path, "eee55555", "Card", f"see {OLD}/x")
    rehome_descriptions(tmp_path, OLD, NEW)
    rehome_descriptions(tmp_path, NEW, OLD)
    assert _folded_description(tmp_path, "eee55555") == f"see {OLD}/x"


def test_rehome_dry_run_reports_without_writing(tmp_path: Path):
    _seed(tmp_path, "fff66666", "Card", f"see {OLD}/x")
    report = rehome_descriptions(tmp_path, OLD, NEW, dry_run=True)
    assert report["matched"] == 1
    assert report["rewritten"] == 0
    assert report["dry_run"] is True
    assert not CardEventLog(tmp_path).read_all()
    assert _folded_description(tmp_path, "fff66666") == f"see {OLD}/x"


def test_rehome_leaves_core_json_write_once(tmp_path: Path):
    _seed(tmp_path, "ggg77777", "Card", f"see {OLD}/x")
    core_path = CardStore(tmp_path).cards_dir / "ggg77777" / "core.json"
    before = core_path.read_text(encoding="utf-8")
    rehome_descriptions(tmp_path, OLD, NEW)
    assert core_path.read_text(encoding="utf-8") == before


def test_rehome_events_are_attributed(tmp_path: Path):
    _seed(tmp_path, "hhh88888", "Card", f"see {OLD}/x")
    rehome_descriptions(tmp_path, OLD, NEW, agent="lumina")
    events = [e for e in CardEventLog(tmp_path).read_all() if e.card_id == "hhh88888"]
    assert len(events) == 1
    assert events[0].action == "describe"
    assert events[0].writer == "lumina"
    assert events[0].title is None  # rehome never touches titles


def test_rehome_empty_prefix_is_refused(tmp_path: Path):
    with pytest.raises(ValueError):
        rehome_descriptions(tmp_path, "", NEW)


def test_rehome_no_matches_is_a_noop(tmp_path: Path):
    _seed(tmp_path, "iii99999", "Card", "nothing to rewrite")
    report = rehome_descriptions(tmp_path, OLD, NEW)
    assert report["matched"] == 0
    assert report["rewritten"] == 0
    assert report["cards"] == []
    assert not CardEventLog(tmp_path).read_all()


def test_find_rehome_matches_uses_the_folded_description(tmp_path: Path):
    """A path introduced by a later describe event is matched, not just core."""
    _seed(tmp_path, "jjj00000", "Card", "original")
    rehome_seed = f"updated: {OLD}/y"
    CardEventLog(tmp_path).append(
        CardEvent(card_id="jjj00000", action="describe", description=rehome_seed, writer="t")
    )
    matches = find_rehome_matches(tmp_path, OLD)
    assert [m["id"] for m in matches] == ["jjj00000"]
    assert matches[0]["description"] == rehome_seed


def test_cli_rehome_rewrites_and_reports(tmp_path: Path):
    _seed(tmp_path, "kkk11111", "Card", f"see {OLD}/x")
    result = CliRunner().invoke(
        _main(),
        ["coord", "rehome", OLD, NEW, "--home", str(tmp_path), "--agent", "lumina"],
    )
    assert result.exit_code == 0, result.output
    assert "Rewrote 1 card(s)" in result.output
    assert _folded_description(tmp_path, "kkk11111") == f"see {NEW}/x"


def test_cli_rehome_dry_run_writes_nothing(tmp_path: Path):
    _seed(tmp_path, "lll22222", "Card", f"see {OLD}/x")
    result = CliRunner().invoke(
        _main(),
        ["coord", "rehome", OLD, NEW, "--home", str(tmp_path), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "Would rewrite 1 card(s)" in result.output
    assert not CardEventLog(tmp_path).read_all()
    assert _folded_description(tmp_path, "lll22222") == f"see {OLD}/x"


def test_cli_rehome_no_matches_still_exits_zero(tmp_path: Path):
    _seed(tmp_path, "mmm33333", "Card", "clean")
    result = CliRunner().invoke(_main(), ["coord", "rehome", OLD, NEW, "--home", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Rewrote 0 card(s)" in result.output
