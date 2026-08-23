"""Tests for the coord status --tag/--parent/--status filters (card 4d03a90a).

Filters must bound the output on both surfaces (CLI and MCP) while leaving
the default unfiltered behavior unchanged.
"""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

from skcapstone.cli.coord import register_coord_commands
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
