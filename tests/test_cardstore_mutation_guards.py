"""CLI and MCP coverage for coreless CardStore mutation rejection."""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from skcapstone.cli.coord import register_coord_commands
from skcapstone.coordination import Board, Task
from skcapstone.mcp_tools import coord_card_tools


def _main() -> click.Group:
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _legacy_only_task(home: Path, monkeypatch, task_id: str) -> None:
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    Board(home).create_task(Task(id=task_id, title="Legacy only"))


def _storage_snapshot(home: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(home)): None if path.is_dir() else path.read_bytes()
        for path in home.rglob("*")
    }


@pytest.mark.parametrize(
    "arguments",
    [
        ["describe", "legacy01", "--description", "changed"],
        ["link", "legacy01", "pr", "https://example.test/1"],
        ["amend-criteria", "legacy01", "--criteria", "changed"],
    ],
)
def test_cli_rejects_coreless_mutation_without_writing(tmp_path, monkeypatch, arguments) -> None:
    _legacy_only_task(tmp_path, monkeypatch, "legacy01")
    before = _storage_snapshot(tmp_path)

    result = CliRunner().invoke(
        _main(), ["coord", *arguments, "--home", str(tmp_path), "--agent", "test-reviewer"]
    )

    assert result.exit_code == 1
    assert "no foldable core" in result.output
    assert _storage_snapshot(tmp_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "arguments"),
    [
        (coord_card_tools._handle_coord_describe, {"description": "changed"}),
        (
            coord_card_tools._handle_coord_link,
            {"key": "pr", "value": "https://example.test/1"},
        ),
        (coord_card_tools._handle_coord_amend_criteria, {"criteria": ["changed"]}),
    ],
)
async def test_mcp_rejects_coreless_mutation_without_writing(
    tmp_path, monkeypatch, handler, arguments
) -> None:
    _legacy_only_task(tmp_path, monkeypatch, "legacy02")
    monkeypatch.setattr(coord_card_tools, "_shared_root", lambda: tmp_path)
    before = _storage_snapshot(tmp_path)

    result = await handler({"task_id": "legacy02", "agent": "test-reviewer", **arguments})
    payload = json.loads(result[0].text)

    assert "no foldable core" in payload["error"]
    assert _storage_snapshot(tmp_path) == before
