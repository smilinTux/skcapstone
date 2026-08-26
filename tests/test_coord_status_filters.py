"""Tests for the coord status --tag/--parent/--status filters (card 4d03a90a).

Filters must bound the output on both surfaces (CLI and MCP) while leaving
the default unfiltered behavior unchanged.
"""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

from skcapstone.card_store import CardCore, CardStore
from skcapstone.cli.coord import register_coord_commands
from skcapstone.coord_eligibility import LeafEligibilityCounts, leaf_eligibility_counts
from skcapstone.coordination import Board, Task
from skcapstone.mcp_tools import coord_tools


def _main() -> click.Group:
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _seed(tmp_path) -> None:
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="epic0001", title="Epic", tags=["sklegal"]))
    board.create_task(Task(id="child001", title="Child A", tags=["parent-epic0001", "sklegal"]))
    board.create_task(Task(id="child002", title="Child B", tags=["parent-epic0001"]))
    board.create_task(Task(id="other001", title="Other", tags=["skchat"]))
    board.claim_task("lumina", "child002")


def test_status_no_filters_lists_everything(tmp_path):
    _seed(tmp_path)
    result = CliRunner().invoke(_main(), ["coord", "status", "--home", str(tmp_path)])
    assert result.exit_code == 0, result.output
    for tid in ("epic0001", "child001", "child002", "other001"):
        assert tid in result.output


def test_status_tag_filter(tmp_path):
    _seed(tmp_path)
    result = CliRunner().invoke(
        _main(), ["coord", "status", "--home", str(tmp_path), "--tag", "sklegal"]
    )
    assert result.exit_code == 0, result.output
    assert "2 total" in result.output
    assert "epic0001" in result.output
    assert "child001" in result.output
    assert "other001" not in result.output


def test_status_parent_filter(tmp_path):
    _seed(tmp_path)
    result = CliRunner().invoke(
        _main(), ["coord", "status", "--home", str(tmp_path), "--parent", "epic0001"]
    )
    assert result.exit_code == 0, result.output
    assert "child001" in result.output
    assert "child002" in result.output
    assert "epic0001" not in result.output
    assert "other001" not in result.output


def test_status_status_filter(tmp_path):
    _seed(tmp_path)
    result = CliRunner().invoke(
        _main(), ["coord", "status", "--home", str(tmp_path), "--status", "in_progress"]
    )
    assert result.exit_code == 0, result.output
    assert "child002" in result.output
    assert "epic0001" not in result.output
    assert "other001" not in result.output


def test_status_combined_filters(tmp_path):
    _seed(tmp_path)
    result = CliRunner().invoke(
        _main(),
        [
            "coord",
            "status",
            "--home",
            str(tmp_path),
            "--parent",
            "epic0001",
            "--status",
            "in_progress",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "child002" in result.output
    assert "child001" not in result.output


def test_status_filter_with_no_match(tmp_path):
    _seed(tmp_path)
    result = CliRunner().invoke(
        _main(), ["coord", "status", "--home", str(tmp_path), "--tag", "nosuchtag"]
    )
    assert result.exit_code == 0, result.output
    assert "No tasks match" in result.output


def _parse(result):
    return json.loads(result[0].text)


@pytest.mark.asyncio
async def test_mcp_status_no_filters(tmp_path, monkeypatch):
    monkeypatch.setattr(coord_tools, "_home", lambda: tmp_path)
    _seed(tmp_path)
    data = _parse(await coord_tools._handle_coord_status({}))
    assert data["summary"]["total"] == 4


@pytest.mark.asyncio
async def test_mcp_status_tag_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(coord_tools, "_home", lambda: tmp_path)
    _seed(tmp_path)
    data = _parse(await coord_tools._handle_coord_status({"tag": ["sklegal"]}))
    ids = {t["id"] for t in data["tasks"]}
    assert ids == {"epic0001", "child001"}
    assert data["summary"]["total"] == 2


@pytest.mark.asyncio
async def test_mcp_status_parent_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(coord_tools, "_home", lambda: tmp_path)
    _seed(tmp_path)
    data = _parse(await coord_tools._handle_coord_status({"parent": "epic0001"}))
    ids = {t["id"] for t in data["tasks"]}
    assert ids == {"child001", "child002"}


@pytest.mark.asyncio
async def test_mcp_status_status_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(coord_tools, "_home", lambda: tmp_path)
    _seed(tmp_path)
    data = _parse(await coord_tools._handle_coord_status({"status": "in_progress"}))
    ids = {t["id"] for t in data["tasks"]}
    assert ids == {"child002"}


@pytest.mark.asyncio
async def test_mcp_status_combined_filters(tmp_path, monkeypatch):
    monkeypatch.setattr(coord_tools, "_home", lambda: tmp_path)
    _seed(tmp_path)
    data = _parse(await coord_tools._handle_coord_status({"parent": "epic0001", "status": "open"}))
    ids = {t["id"] for t in data["tasks"]}
    assert ids == {"child001"}


def test_status_leaf_eligible_known_mix_uses_folded_cards(tmp_path):
    """Known noise, gate, container, dependency, and ownership cases are excluded."""
    store = CardStore(tmp_path)

    def add(card_id, title, *, kind="task", labels=(), dependencies=()):
        """Create one immutable fixture card."""
        store.create(
            CardCore(
                id=card_id,
                kind=kind,
                title=title,
                initial_labels=list(labels),
                dependencies=list(dependencies),
            )
        )

    add("done0001", "Completed dependency")
    store.append_event("done0001", "complete", "fixture")
    add("leaf0001", "Genuine leaf", dependencies=("done0001",))
    add("human01", "[HUMAN] Approval")
    add("humantag", "Approval", labels=("human-gate",))
    add("parent01", "Parent")
    add("child001", "Child", labels=("parent-parent01", "do-not-claim"))
    add("epic0001", "Epic", kind="epic")
    add("sprint01", "Sprint", labels=("sprint-container",))
    add("sprint02", "[AREA][SPRINT 1] Sprint by title")
    add("blocked1", "Blocked", dependencies=("missing1",))
    add("super01", "Superseded", labels=("superseded",))
    add("noclaim1", "Do not claim", labels=("do-not-claim",))
    add("noclaim2", "Not claimable", labels=("not-claimable",))
    add("owned001", "Owned")
    store.append_event("owned001", "assign", "fixture", owner="agent")
    add("review01", "Needs reviewer")
    store.append_event("review01", "move", "fixture", column="review")
    add("noise001", "x")

    result = CliRunner().invoke(_main(), ["coord", "status", "--home", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "1 leaf eligible" in result.output
    assert "1 review needs identity" in result.output
    assert leaf_eligibility_counts(tmp_path) == LeafEligibilityCounts(
        leaves=1, review=1, malformed=1
    )


def test_status_uses_same_folded_dependencies_as_claim_gate(tmp_path):
    """Status must show a folded dependency that makes claim fail closed."""
    store = CardStore(tmp_path)
    store.create(CardCore(id="gate0001", title="Incomplete gate"))
    store.create(CardCore(id="target01", title="Folded dependency target"))
    store.append_event(
        "target01",
        "add_dependency",
        "governance",
        dependency="gate0001",
        reason="test gate",
    )

    result = CliRunner().invoke(_main(), ["coord", "status", "--home", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "target01" in result.output
    target_line = next(line for line in result.output.splitlines() if "target01" in line)
    assert "BLOCKED" in target_line
    with pytest.raises(ValueError, match="incomplete dependencies: gate0001"):
        Board(tmp_path).claim_task("worker", "target01")
