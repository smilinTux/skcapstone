"""Tests for the coord void verb (card 325a737f).

A voided card must leave the active board WITHOUT a completion event:
no Joules minted, no changelog entry, but still foldable for audit with
its writer-attributed void reason.
"""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

import skcapstone.coordination as coordination
from skcapstone.card import KanbanBoard
from skcapstone.card_store import CardCore, CardStore
from skcapstone.changelog import generate_changelog
from skcapstone.cli.coord import register_coord_commands
from skcapstone.coord_amendments import is_voided, void_record
from skcapstone.coordination import Board, Task
from skcapstone.mcp_tools import coord_card_tools


def _main() -> click.Group:
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _seed(tmp_path, task_id: str, title: str = "Card") -> None:
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id=task_id, title=title))
    CardStore(tmp_path).create(CardCore(id=task_id, title=title))


def _void_args(task_id: str, tmp_path, reason: str = "created by mistake") -> list[str]:
    return [
        "coord",
        "void",
        task_id,
        "--home",
        str(tmp_path),
        "--reason",
        reason,
        "--agent",
        "lumina",
    ]


def test_void_removes_card_from_active_board(tmp_path):
    _seed(tmp_path, "void0001")
    result = CliRunner().invoke(_main(), _void_args("void0001", tmp_path))
    assert result.exit_code == 0, result.output
    assert all(c.id != "void0001" for c in KanbanBoard(tmp_path).cards())
    assert all(v.task.id != "void0001" for v in Board(tmp_path).get_task_views())


def test_void_mints_no_joules(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        coordination, "_mint_joules_for_task", lambda b, tid, agent: calls.append(tid)
    )
    _seed(tmp_path, "void0002")
    _seed(tmp_path, "keep0002", title="Real work")
    result = CliRunner().invoke(_main(), _void_args("void0002", tmp_path))
    assert result.exit_code == 0, result.output
    assert calls == []
    # Completing a different card still mints, proving the spy is live.
    Board(tmp_path).complete_task("lumina", "keep0002")
    assert calls == ["keep0002"]


def test_void_keeps_card_out_of_changelog(tmp_path, monkeypatch):
    monkeypatch.setattr(coordination, "_mint_joules_for_task", lambda b, tid, agent: None)
    _seed(tmp_path, "void0003", title="Mistaken card")
    _seed(tmp_path, "keep0003", title="Real work")
    assert CliRunner().invoke(_main(), _void_args("void0003", tmp_path)).exit_code == 0
    Board(tmp_path).complete_task("lumina", "keep0003")
    changelog = generate_changelog(tmp_path)
    assert "Real work" in changelog
    assert "Mistaken card" not in changelog


def test_voided_card_remains_auditable(tmp_path):
    _seed(tmp_path, "void0004")
    assert (
        CliRunner()
        .invoke(_main(), _void_args("void0004", tmp_path, "fat-fingered create"))
        .exit_code
        == 0
    )
    archived = {c.id: c for c in KanbanBoard(tmp_path).cards(include_archived=True)}
    assert "void0004" in archived
    assert archived["void0004"].archived is True

    record = void_record(tmp_path, "void0004")
    assert record is not None
    assert record["writer"] == "lumina"
    assert record["reason"] == "fat-fingered create"
    assert is_voided(tmp_path, "void0004") is True


def test_void_requires_a_reason(tmp_path):
    _seed(tmp_path, "void0005")
    result = CliRunner().invoke(_main(), ["coord", "void", "void0005", "--home", str(tmp_path)])
    assert result.exit_code != 0
    assert is_voided(tmp_path, "void0005") is False


def test_void_twice_is_refused(tmp_path):
    _seed(tmp_path, "void0006")
    runner = CliRunner()
    assert runner.invoke(_main(), _void_args("void0006", tmp_path)).exit_code == 0
    result = runner.invoke(_main(), _void_args("void0006", tmp_path))
    assert result.exit_code != 0
    assert "already voided" in result.output


# -- MCP twin -----------------------------------------------------------------


def _parse(result):
    return json.loads(result[0].text)


@pytest.mark.asyncio
async def test_mcp_void(tmp_path, monkeypatch):
    monkeypatch.setattr(coord_card_tools, "_shared_root", lambda: tmp_path)
    _seed(tmp_path, "void0mcp")
    result = await coord_card_tools._handle_coord_void(
        {"task_id": "void0mcp", "reason": "mistake", "agent": "lumina"}
    )
    assert _parse(result)["voided"] is True
    assert all(c.id != "void0mcp" for c in KanbanBoard(tmp_path).cards())
    assert void_record(tmp_path, "void0mcp")["reason"] == "mistake"


@pytest.mark.asyncio
async def test_mcp_void_requires_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(coord_card_tools, "_shared_root", lambda: tmp_path)
    _seed(tmp_path, "void0mcq")
    result = await coord_card_tools._handle_coord_void({"task_id": "void0mcq"})
    assert "error" in _parse(result)
    assert is_voided(tmp_path, "void0mcq") is False
