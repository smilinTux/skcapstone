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
from packaging.version import Version
from skcoord import card_store as skcoord_card_store

from skcapstone.card import KanbanBoard
from skcapstone.card_store import CardCore, CardStore, parity_check
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


def test_skcoord_pins_agree_between_pyproject_and_ci():
    """The declared floor and the CI registry pin must be the SAME version.

    tests/test_coord_amend.py runs twice in the pytest workflow: once against the
    registry wheel that step force-installs, and again against skcoord from git
    main. If those implement different contracts, no assertion in this file can
    satisfy both and the job is unwinnable by anyone.

    This test used to hardcode the version in a third place, which is precisely
    how the drift became invisible: the CI pin sat at 0.1.44 while skcoord
    released through 0.1.53, and this test kept asserting 0.1.44 was correct. On
    2026-08-27 skcoord 0.1.51 changed KanbanBoard.cards() from failing closed on
    malformed criteria to degrading the one unreadable card, the two CI steps
    began asserting opposite behaviours, and every open PR went red without
    having touched the code.

    So it now derives the expected version from pyproject and asserts CI matches,
    rather than naming a version of its own. Bumping skcoord is then a two-file
    change that this test verifies, instead of a three-file change where forgetting
    the third is silent.
    """
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [Requirement(value) for value in project["project"]["dependencies"]]
    skcoord = next(requirement for requirement in requirements if requirement.name == "skcoord")

    floors = [Version(spec.version) for spec in skcoord.specifier if spec.operator in (">=", "==")]
    assert floors, "skcoord must declare an explicit floor, not float on any release"
    declared = max(floors)

    # the floor is a real gate: the release below it must be excluded
    below = Version(f"{declared.major}.{declared.minor}.{max(declared.micro - 1, 0)}")
    assert below not in skcoord.specifier
    assert declared in skcoord.specifier

    workflow = (PROJECT_ROOT / ".github" / "workflows" / "pytest.yml").read_text(encoding="utf-8")
    assert f'"skcoord=={declared}"' in workflow, (
        f"CI registry pin must equal the pyproject floor {declared}; "
        "a stale pin makes the two test runs assert different contracts"
    )
    assert f'Version(version("skcoord")) == Version("{declared}")' in workflow


@pytest.mark.parametrize("mode", [None, "1", "dual", "0", "off", "false", "no"])
def test_every_card_store_selector_projects_current_criteria(tmp_path, monkeypatch, mode):
    birth = [f"birth criterion {index}" for index in range(1, 10)]
    current = [f"current criterion {index}" for index in range(1, 10)]
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    _seed(tmp_path, "criteria9", criteria=birth)
    task_before = next((tmp_path / "coordination" / "tasks").glob("criteria9-*.json")).read_bytes()
    core_before = (CardStore(tmp_path).cards_dir / "criteria9" / "core.json").read_bytes()
    CardStore(tmp_path).append_event(
        "criteria9", "amend_criteria", "uptake-test", criteria=current
    )

    if mode is None:
        monkeypatch.delenv("SKCOORD_CARD_STORE", raising=False)
    else:
        monkeypatch.setenv("SKCOORD_CARD_STORE", mode)
    projected = next(card for card in KanbanBoard(tmp_path).cards() if card.id == "criteria9")

    assert projected.acceptance_criteria == current
    assert (
        next((tmp_path / "coordination" / "tasks").glob("criteria9-*.json")).read_bytes()
        == task_before
    )
    assert (CardStore(tmp_path).cards_dir / "criteria9" / "core.json").read_bytes() == core_before


# Legacy-read selectors still refuse the board outright. Store-read selectors
# degrade the one bad card instead, since skcoord 0.1.51 (skcoord #52,
# "fix(cardstore): isolate unreadable card folds") made KanbanBoard.cards() pass
# degrade_unreadable=True so one corrupt stream cannot blank the whole board.
_LEGACY_READ_SELECTORS = ["dual", "0", "off", "false", "no"]
_STORE_READ_SELECTORS = [None, "1"]


def _seed_malformed_criteria(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    _seed(tmp_path, "badcriteria", criteria=["birth criterion"])
    CardStore(tmp_path).append_event("badcriteria", "amend_criteria", "uptake-test", criteria=[])
    if mode is None:
        monkeypatch.delenv("SKCOORD_CARD_STORE", raising=False)
    else:
        monkeypatch.setenv("SKCOORD_CARD_STORE", mode)


@pytest.mark.parametrize("mode", _LEGACY_READ_SELECTORS)
def test_legacy_read_selectors_fail_closed_on_malformed_criteria(tmp_path, monkeypatch, mode):
    _seed_malformed_criteria(tmp_path, monkeypatch, mode)

    with pytest.raises(ValueError, match="criteria"):
        KanbanBoard(tmp_path).cards()


@pytest.mark.parametrize("mode", _STORE_READ_SELECTORS)
def test_store_read_selectors_surface_malformed_criteria_loudly(tmp_path, monkeypatch, mode):
    """The card must never project as if it were healthy.

    The safety property this file has always asserted is that malformed criteria
    cannot pass silently. Criteria are what "done" is measured against, so a card
    whose criteria were dropped must not look like an ordinary card.

    skcoord 0.1.51 changed HOW that is enforced on the store-read path, from
    refusing the whole board to degrading the single bad card. The property still
    holds, and this asserts it in the new form rather than deleting it: the card
    is projected as UNREADABLE, flagged critical, labelled, and carries the
    reason. What is NOT acceptable, and what this test would catch, is the card
    appearing with its criteria quietly emptied.
    """
    _seed_malformed_criteria(tmp_path, monkeypatch, mode)

    cards = KanbanBoard(tmp_path).cards()
    bad = next(card for card in cards if card.id == "badcriteria")

    assert bad.meta.get("unreadable") is True
    assert "unreadable" in bad.labels
    assert bad.title.startswith("UNREADABLE")
    assert bad.priority == "critical"
    assert "criteria" in str(bad.meta.get("reason", "")).lower()
    # and the failure is attributable, not anonymous
    assert bad.meta.get("source") == "cards/badcriteria"


def test_criteria_projection_assertion_is_sensitive_to_fold_bypass(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    _seed(tmp_path, "sensitive9", criteria=["birth criterion"])
    CardStore(tmp_path).append_event(
        "sensitive9", "amend_criteria", "uptake-test", criteria=["current criterion"]
    )

    def stale_criteria(home, card_id, birth_criteria=None, store=None):
        del home, card_id, store
        return list(birth_criteria or [])

    monkeypatch.setattr(skcoord_card_store, "current_acceptance_criteria", stale_criteria)
    result = parity_check(tmp_path)
    mismatch = next(item for item in result["mismatches"] if item["id"] == "sensitive9")

    assert mismatch["diff"]["acceptance_criteria"] == [
        ["birth criterion"],
        ["current criterion"],
    ]


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


def test_amend_criteria_fails_loudly_when_append_does_not_reach_independent_fold(
    tmp_path, monkeypatch
):
    _seed(tmp_path, "acnooped", criteria=["original"])
    monkeypatch.setattr(CardStore, "append_event", lambda *args, **kwargs: {})

    result = CliRunner().invoke(
        _main(),
        [
            "coord",
            "amend-criteria",
            "acnooped",
            "--home",
            str(tmp_path),
            "--criteria",
            "claimed replacement",
            "--agent",
            "lumina",
        ],
    )

    assert result.exit_code == 1
    assert "did not persist" in result.output
    assert "Amended criteria" not in result.output
    # Different read path from append_event: the board projection folds the
    # store and must retain the old value when the writer silently does nothing.
    view = next(view for view in Board(tmp_path).get_task_views() if view.task.id == "acnooped")
    assert view.task.acceptance_criteria == ["original"]


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


def test_amend_criteria_rejects_unknown_card_without_creating_event_log(tmp_path):
    result = CliRunner().invoke(
        _main(),
        [
            "coord",
            "amend-criteria",
            "unknown1",
            "--home",
            str(tmp_path),
            "--criteria",
            "replacement",
        ],
    )

    assert result.exit_code == 1
    assert "no foldable core" in result.output
    assert not (CardStore(tmp_path).cards_dir / "unknown1").exists()


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
async def test_mcp_amend_criteria_reports_silent_append_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(coord_card_tools, "_shared_root", lambda: tmp_path)
    _seed(tmp_path, "acnoopmcp", criteria=["original"])
    monkeypatch.setattr(CardStore, "append_event", lambda *args, **kwargs: {})

    result = await coord_card_tools._handle_coord_amend_criteria(
        {"task_id": "acnoopmcp", "criteria": ["claimed replacement"]}
    )

    assert "did not persist" in _parse(result)["error"]
    view = next(
        view for view in Board(tmp_path).get_task_views() if view.task.id == "acnoopmcp"
    )
    assert view.task.acceptance_criteria == ["original"]


@pytest.mark.asyncio
async def test_mcp_amend_criteria_requires_criteria(tmp_path, monkeypatch):
    monkeypatch.setattr(coord_card_tools, "_shared_root", lambda: tmp_path)
    _seed(tmp_path, "ac0000mcq")
    result = await coord_card_tools._handle_coord_amend_criteria(
        {"task_id": "ac0000mcq", "criteria": []}
    )
    assert "error" in _parse(result)
