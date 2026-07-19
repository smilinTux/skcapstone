"""Tests for the coord_score MCP tool + handler."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from skcapstone.coordination import Board, Task
from skcapstone.mcp_tools import coord_tools


def _parse(result):
    return json.loads(result[0].text)


def test_coord_score_tool_registered():
    names = {t.name for t in coord_tools.TOOLS}
    assert "coord_score" in names
    assert "coord_score" in coord_tools.HANDLERS


@pytest.mark.asyncio
async def test_handle_coord_score_writes_meta(tmp_path: Path):
    board = Board(tmp_path)
    board.create_task(Task(id="abc12345", title="MCP score"))
    with patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(tmp_path)):
        result = await coord_tools._handle_coord_score({
            "task_id": "abc12345", "round": 2, "score": 4,
            "notes": "n", "harness": "claude_code", "phase": "grade",
        })
    parsed = _parse(result)
    assert parsed["scored"] is True
    ap = {x.id: x for x in board.load_tasks()}["abc12345"].meta["autopilot"]
    assert ap["scores"][0]["round"] == 2
    assert ap["scores"][0]["score"] == 4


@pytest.mark.asyncio
async def test_handle_coord_score_requires_params(tmp_path: Path):
    with patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(tmp_path)):
        result = await coord_tools._handle_coord_score({"task_id": "abc12345"})
    assert "error" in _parse(result)
