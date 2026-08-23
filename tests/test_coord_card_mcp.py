"""Tests for the coord_describe / coord_label / coord_link MCP tools.

Each verb must be callable through the MCP tool layer and produce exactly
the same append-only overlay event as the CLI path (card 61b97e22).
"""

from __future__ import annotations

import json

import pytest

from skcapstone.card import CardEventLog, KanbanBoard
from skcapstone.card_store import CardCore, CardStore
from skcapstone.coordination import Board, Task
from skcapstone.mcp_tools import coord_card_tools


def _parse(result):
    return json.loads(result[0].text)


def _seed(tmp_path, task_id: str, title: str = "Card", description: str = "original") -> None:
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id=task_id, title=title, description=description))
    CardStore(tmp_path).create(CardCore(id=task_id, title=title, description=description))


def test_card_tools_registered():
    names = {t.name for t in coord_card_tools.TOOLS}
    assert {"coord_describe", "coord_label", "coord_link"} <= names
    for name in ("coord_describe", "coord_label", "coord_link"):
        assert name in coord_card_tools.HANDLERS


@pytest.mark.asyncio
async def test_describe_handler_appends_the_same_event_as_the_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(coord_card_tools, "_shared_root", lambda: tmp_path)
    _seed(tmp_path, "mcp00001")
    result = await coord_card_tools._handle_coord_describe(
        {"task_id": "mcp00001", "description": "edited via mcp", "agent": "lumina"}
    )
    data = _parse(result)
    assert data["described"] is True
    assert data["changed"] == ["description"]

    events = [e for e in CardEventLog(tmp_path).read_all() if e.card_id == "mcp00001"]
    assert len(events) == 1
    assert events[0].action == "describe"
    assert events[0].writer == "lumina"
    assert events[0].description == "edited via mcp"
    assert events[0].title is None

    card = next(c for c in KanbanBoard(tmp_path).cards() if c.id == "mcp00001")
    assert card.description == "edited via mcp"
    assert card.title == "Card"


@pytest.mark.asyncio
async def test_describe_handler_requires_a_field(tmp_path, monkeypatch):
    monkeypatch.setattr(coord_card_tools, "_shared_root", lambda: tmp_path)
    _seed(tmp_path, "mcp00002")
    result = await coord_card_tools._handle_coord_describe({"task_id": "mcp00002"})
    assert "error" in _parse(result)
    assert not CardEventLog(tmp_path).read_all()


@pytest.mark.asyncio
async def test_label_handler_adds_and_removes(tmp_path, monkeypatch):
    monkeypatch.setattr(coord_card_tools, "_shared_root", lambda: tmp_path)
    _seed(tmp_path, "mcp00003")

    add = await coord_card_tools._handle_coord_label(
        {"task_id": "mcp00003", "label": "urgent", "agent": "lumina"}
    )
    assert _parse(add)["action"] == "add_label"
    card = next(c for c in KanbanBoard(tmp_path).cards() if c.id == "mcp00003")
    assert "urgent" in card.labels

    rem = await coord_card_tools._handle_coord_label(
        {"task_id": "mcp00003", "label": "urgent", "remove": True, "agent": "lumina"}
    )
    assert _parse(rem)["action"] == "remove_label"
    card = next(c for c in KanbanBoard(tmp_path).cards() if c.id == "mcp00003")
    assert "urgent" not in card.labels

    events = [e for e in CardEventLog(tmp_path).read_all() if e.card_id == "mcp00003"]
    assert [e.action for e in events] == ["add_label", "remove_label"]
    assert all(e.writer == "lumina" for e in events)


@pytest.mark.asyncio
async def test_link_handler_attaches_a_link(tmp_path, monkeypatch):
    monkeypatch.setattr(coord_card_tools, "_shared_root", lambda: tmp_path)
    _seed(tmp_path, "mcp00004")
    result = await coord_card_tools._handle_coord_link(
        {
            "task_id": "mcp00004",
            "key": "pr",
            "value": "https://example.test/pr/1",
            "agent": "lumina",
        }
    )
    assert _parse(result)["linked"] is True

    events = [e for e in CardEventLog(tmp_path).read_all() if e.card_id == "mcp00004"]
    assert len(events) == 1
    assert events[0].action == "link"
    assert events[0].link_key == "pr"
    assert events[0].link_value == "https://example.test/pr/1"

    card = next(c for c in KanbanBoard(tmp_path).cards() if c.id == "mcp00004")
    assert card.links["pr"] == "https://example.test/pr/1"


@pytest.mark.asyncio
async def test_handlers_validate_required_args(tmp_path, monkeypatch):
    monkeypatch.setattr(coord_card_tools, "_shared_root", lambda: tmp_path)
    assert "error" in _parse(await coord_card_tools._handle_coord_describe({}))
    assert "error" in _parse(await coord_card_tools._handle_coord_label({"task_id": "x"}))
    assert "error" in _parse(
        await coord_card_tools._handle_coord_link({"task_id": "x", "key": "pr"})
    )
