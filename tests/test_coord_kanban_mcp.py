"""Tests for the coord_kanban + coord_move MCP tools."""
from __future__ import annotations

import json

import pytest

from skcapstone.coordination import Board, Task
from skcapstone.mcp_tools import coord_tools


def _parse(result):
    return json.loads(result[0].text)


def test_kanban_and_move_tools_registered():
    names = {t.name for t in coord_tools.TOOLS}
    assert "coord_kanban" in names
    assert "coord_move" in names
    assert "coord_kanban" in coord_tools.HANDLERS
    assert "coord_move" in coord_tools.HANDLERS


@pytest.mark.asyncio
async def test_coord_kanban_handler_returns_grid(tmp_path, monkeypatch):
    monkeypatch.setattr(coord_tools, "_shared_root", lambda: tmp_path)
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="km1", title="kanban card", created_by="opus"))
    result = await coord_tools._handle_coord_kanban({})
    data = _parse(result)
    assert "counts" in data
    assert "wip" in data
    assert data["totals"]["active"] >= 1


@pytest.mark.asyncio
async def test_coord_move_handler_moves_card(tmp_path, monkeypatch):
    monkeypatch.setattr(coord_tools, "_shared_root", lambda: tmp_path)
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="km2", title="movable", created_by="opus"))
    result = await coord_tools._handle_coord_move(
        {"task_id": "km2", "column": "doing"}
    )
    data = _parse(result)
    assert data["moved"] is True
    # verify it took effect on the board
    from skcapstone.card import KanbanBoard
    card = next(c for c in KanbanBoard(tmp_path).cards() if c.id == "km2")
    assert card.status.value == "doing"


@pytest.mark.asyncio
async def test_coord_move_handler_rejects_bad_column(tmp_path, monkeypatch):
    monkeypatch.setattr(coord_tools, "_shared_root", lambda: tmp_path)
    result = await coord_tools._handle_coord_move({"task_id": "x", "column": "bogus"})
    data = _parse(result)
    assert "error" in data or data.get("moved") is not True
