"""Tests for the folded amendment verbs (card e78fd954).

``coord reprioritize`` and ``coord amend-criteria`` (plus their MCP twins)
append writer-attributed events that the fold applies on read. Birth facts
stay write-once: ``core.json`` must be byte-identical after an amendment,
and re-applying reverses the change.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
import tomllib
from click.testing import CliRunner
from packaging.requirements import Requirement

from skcapstone.card import KanbanBoard
from skcapstone.card_store import CardCore, CardStore
from skcapstone.cli.coord import register_coord_commands
from skcapstone.coord_amendments import current_acceptance_criteria
from skcapstone.coordination import Board, Task
from skcapstone.mcp_tools import coord_card_tools

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _main() -> click.Group:
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _seed(tmp_path, task_id: str, priority: str = "medium", criteria=()) -> None:
    from skcapstone.coordination import TaskPriority

    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(
        Task(
            id=task_id,
            title="Card",
            priority=TaskPriority(priority),
            acceptance_criteria=list(criteria),
        )
    )
    CardStore(tmp_path).create(
        CardCore(id=task_id, title="Card", acceptance_criteria=list(criteria))
    )


def _core_text(tmp_path, task_id: str) -> str:
    return (CardStore(tmp_path).cards_dir / task_id / "core.json").read_text(encoding="utf-8")


def _assert_authoritative_criteria(tmp_path, task_id: str, expected: list[str]) -> None:
    card = CardStore(tmp_path).fold(task_id)
    assert card is not None
    assert card.acceptance_criteria == expected

    view = next(view for view in Board(tmp_path).get_task_views() if view.task.id == task_id)
    assert view.task.acceptance_criteria == expected


def test_skcoord_dependency_requires_scheduled_reconcile_policy():
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [Requirement(value) for value in project["project"]["dependencies"]]
    skcoord = next(requirement for requirement in requirements if requirement.name == "skcoord")

    assert "0.1.27" not in skcoord.specifier
    assert "0.1.36" in skcoord.specifier


def test_current_acceptance_criteria_delegates_to_card_store_fold(tmp_path, monkeypatch):
    calls = []

    class FoldedCard:
        acceptance_criteria = ["authoritative criterion"]

    def fold(_store, task_id):
        calls.append(task_id)
        return FoldedCard()

    monkeypatch.setattr(CardStore, "fold", fold)

    assert current_acceptance_criteria(tmp_path, "fold0001") == ["authoritative criterion"]
    assert calls == ["fold0001"]


# -- reprioritize (CLI) ------------------------------------------------------


def test_reprioritize_updates_fold_and_leaves_core_json(tmp_path):
    _seed(tmp_path, "rp000001", priority="medium")
    before = _core_text(tmp_path, "rp000001")
    result = CliRunner().invoke(
        _main(),
        [
            "coord",
            "reprioritize",
            "rp000001",
            "--home",
            str(tmp_path),
            "--priority",
            "high",
            "--agent",
            "lumina",
        ],
    )
    assert result.exit_code == 0, result.output
    card = next(c for c in KanbanBoard(tmp_path).cards() if c.id == "rp000001")
    assert card.priority == "high"
    assert _core_text(tmp_path, "rp000001") == before


def test_reprioritize_is_reversible_by_reapplying(tmp_path):
    _seed(tmp_path, "rp000002", priority="low")
    runner = CliRunner()
    for priority in ("critical", "low"):
        result = runner.invoke(
            _main(),
            [
                "coord",
                "reprioritize",
                "rp000002",
                "--home",
                str(tmp_path),
                "--priority",
                priority,
                "--agent",
                "lumina",
            ],
        )
        assert result.exit_code == 0, result.output
    card = next(c for c in KanbanBoard(tmp_path).cards() if c.id == "rp000002")
    assert card.priority == "low"


def test_reprioritize_rejects_bad_priority(tmp_path):
    _seed(tmp_path, "rp000003")
    result = CliRunner().invoke(
        _main(),
        ["coord", "reprioritize", "rp000003", "--home", str(tmp_path), "--priority", "bogus"],
    )
    assert result.exit_code != 0


# -- amend-criteria (CLI) ----------------------------------------------------


def test_amend_criteria_replaces_the_folded_list(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    _seed(tmp_path, "ac000001", criteria=["original one", "original two"])
    before = _core_text(tmp_path, "ac000001")
    result = CliRunner().invoke(
        _main(),
        [
            "coord",
            "amend-criteria",
            "ac000001",
            "--home",
            str(tmp_path),
            "--criteria",
            "sharper one",
            "--agent",
            "lumina",
        ],
    )
    assert result.exit_code == 0, result.output
    assert current_acceptance_criteria(tmp_path, "ac000001") == ["sharper one"]
    _assert_authoritative_criteria(tmp_path, "ac000001", ["sharper one"])
    assert _core_text(tmp_path, "ac000001") == before


def test_amend_criteria_is_reversible_by_reapplying(tmp_path):
    _seed(tmp_path, "ac000002", criteria=["original"])
    runner = CliRunner()
    for criteria in (["amended"], ["original"]):
        args = ["coord", "amend-criteria", "ac000002", "--home", str(tmp_path), "--agent", "x"]
        for c in criteria:
            args += ["--criteria", c]
        assert runner.invoke(_main(), args).exit_code == 0
    assert current_acceptance_criteria(tmp_path, "ac000002") == ["original"]


def test_amend_criteria_requires_at_least_one_criterion(tmp_path):
    _seed(tmp_path, "ac000003")
    result = CliRunner().invoke(
        _main(), ["coord", "amend-criteria", "ac000003", "--home", str(tmp_path)]
    )
    assert result.exit_code != 0
    assert current_acceptance_criteria(tmp_path, "ac000003") == []


def test_amend_criteria_event_is_writer_attributed(tmp_path):
    _seed(tmp_path, "ac000004")
    CliRunner().invoke(
        _main(),
        [
            "coord",
            "amend-criteria",
            "ac000004",
            "--home",
            str(tmp_path),
            "--criteria",
            "x",
            "--agent",
            "lumina",
        ],
    )
    events = CardStore(tmp_path)._read_events("ac000004")
    amend_events = [e for e in events if e.get("action") == "amend_criteria"]
    assert len(amend_events) == 1
    assert amend_events[0]["writer"] == "lumina"
    assert amend_events[0]["criteria"] == ["x"]


# -- MCP twins ----------------------------------------------------------------


def _parse(result):
    return json.loads(result[0].text)


@pytest.mark.asyncio
async def test_mcp_reprioritize(tmp_path, monkeypatch):
    monkeypatch.setattr(coord_card_tools, "_shared_root", lambda: tmp_path)
    _seed(tmp_path, "rp0000mcp", priority="medium")
    result = await coord_card_tools._handle_coord_reprioritize(
        {"task_id": "rp0000mcp", "priority": "high", "agent": "lumina"}
    )
    assert _parse(result)["reprioritized"] is True
    card = next(c for c in KanbanBoard(tmp_path).cards() if c.id == "rp0000mcp")
    assert card.priority == "high"


@pytest.mark.asyncio
async def test_mcp_reprioritize_rejects_bad_priority(tmp_path, monkeypatch):
    monkeypatch.setattr(coord_card_tools, "_shared_root", lambda: tmp_path)
    _seed(tmp_path, "rp0000mcq")
    result = await coord_card_tools._handle_coord_reprioritize(
        {"task_id": "rp0000mcq", "priority": "bogus"}
    )
    assert "error" in _parse(result)


@pytest.mark.asyncio
async def test_mcp_amend_criteria(tmp_path, monkeypatch):
    monkeypatch.setattr(coord_card_tools, "_shared_root", lambda: tmp_path)
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    _seed(tmp_path, "ac0000mcp", criteria=["original"])
    before = _core_text(tmp_path, "ac0000mcp")
    result = await coord_card_tools._handle_coord_amend_criteria(
        {"task_id": "ac0000mcp", "criteria": ["a", "b"], "agent": "lumina"}
    )
    data = _parse(result)
    assert data["amended"] is True
    assert data["acceptance_criteria"] == ["a", "b"]
    assert current_acceptance_criteria(tmp_path, "ac0000mcp") == ["a", "b"]
    _assert_authoritative_criteria(tmp_path, "ac0000mcp", ["a", "b"])
    assert _core_text(tmp_path, "ac0000mcp") == before


@pytest.mark.asyncio
async def test_mcp_amend_criteria_requires_criteria(tmp_path, monkeypatch):
    monkeypatch.setattr(coord_card_tools, "_shared_root", lambda: tmp_path)
    _seed(tmp_path, "ac0000mcq")
    result = await coord_card_tools._handle_coord_amend_criteria(
        {"task_id": "ac0000mcq", "criteria": []}
    )
    assert "error" in _parse(result)
