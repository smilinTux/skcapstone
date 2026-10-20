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
        ["describe", "1e9ac101", "--description", "changed"],
        ["label", "1e9ac101", "reviewed"],
        ["link", "1e9ac101", "pr", "https://example.test/1"],
        ["amend-criteria", "1e9ac101", "--criteria", "changed"],
    ],
)
def test_cli_rejects_coreless_mutation_without_writing(tmp_path, monkeypatch, arguments) -> None:
    _legacy_only_task(tmp_path, monkeypatch, "1e9ac101")
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
        (coord_card_tools._handle_coord_label, {"label": "reviewed"}),
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
    _legacy_only_task(tmp_path, monkeypatch, "1e9ac102")
    monkeypatch.setattr(coord_card_tools, "_shared_root", lambda: tmp_path)
    before = _storage_snapshot(tmp_path)

    result = await handler({"task_id": "1e9ac102", "agent": "test-reviewer", **arguments})
    payload = json.loads(result[0].text)

    assert "no foldable core" in payload["error"]
    assert _storage_snapshot(tmp_path) == before


_INVALID_IDS = [
    "",
    "deadbeeg",
    "POOL_IDS|chiap08|ids=12345678",
    "inc_e2ec1a1a",
    "toolong-e2ec1a1a",
    "../e2ec1a1a",
    "e2ec1a1a\n",
    "a" * 65,
]


@pytest.mark.parametrize("task_id", _INVALID_IDS)
@pytest.mark.parametrize("verb", ["label", "link"])
def test_cli_rejects_non_card_identifiers_without_writing(tmp_path, task_id, verb) -> None:
    before = _storage_snapshot(tmp_path)
    arguments = [verb, task_id, "reviewed"]
    if verb == "link":
        arguments.append("PASS")

    result = CliRunner().invoke(
        _main(), ["coord", *arguments, "--home", str(tmp_path), "--agent", "test-reviewer"]
    )

    assert result.exit_code != 0
    assert "must be a card id" in result.output
    assert _storage_snapshot(tmp_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("task_id", _INVALID_IDS)
@pytest.mark.parametrize(
    "handler", [coord_card_tools._handle_coord_label, coord_card_tools._handle_coord_link]
)
async def test_mcp_rejects_non_card_identifiers_without_writing(
    tmp_path, monkeypatch, task_id, handler
) -> None:
    monkeypatch.setattr(coord_card_tools, "_shared_root", lambda: tmp_path)
    before = _storage_snapshot(tmp_path)
    arguments = {
        "task_id": task_id,
        "label": "reviewed",
        "key": "result",
        "value": "PASS",
        "agent": "test-reviewer",
    }

    result = await handler(arguments)

    expected = "required" if task_id == "" else "must be a card id"
    assert expected in json.loads(result[0].text).get("error", "")
    assert _storage_snapshot(tmp_path) == before


_LIVE_IDS = ["inc-e2ec1a1a", "rev-ff58cdd5", "7a0c01", "ABCDEF12", "deadbeef0"]


@pytest.mark.parametrize("task_id", _LIVE_IDS)
def test_cli_label_and_link_accept_the_id_shapes_create_accepts(tmp_path, task_id) -> None:
    runner = CliRunner()
    base = ["--home", str(tmp_path), "--agent", "test-reviewer"]
    created = runner.invoke(
        _main(),
        ["coord", "create", "--title", "Live id card", "--id", task_id, "--home", str(tmp_path)],
    )
    assert created.exit_code == 0, created.output

    labelled = runner.invoke(_main(), ["coord", "label", task_id, "reviewed", *base])
    linked = runner.invoke(_main(), ["coord", "link", task_id, "result", "PASS", *base])

    assert labelled.exit_code == 0, labelled.output
    assert linked.exit_code == 0, linked.output


@pytest.mark.parametrize("task_id", ["inc-e2ec1a1a", "rev-ff58cdd5", "7a0c01"])
def test_cli_valid_shape_without_core_is_still_refused(tmp_path, task_id) -> None:
    before = _storage_snapshot(tmp_path)

    result = CliRunner().invoke(
        _main(),
        ["coord", "label", task_id, "x", "--home", str(tmp_path), "--agent", "test-reviewer"],
    )

    assert result.exit_code == 1
    assert "no foldable core" in result.output
    assert _storage_snapshot(tmp_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("task_id", ["inc-e2ec1a1a", "rev-ff58cdd5", "7a0c01"])
async def test_mcp_valid_shape_without_core_is_refused_for_the_right_reason(
    tmp_path, monkeypatch, task_id
) -> None:
    monkeypatch.setattr(coord_card_tools, "_shared_root", lambda: tmp_path)

    result = await coord_card_tools._handle_coord_label(
        {"task_id": task_id, "label": "x", "agent": "test-reviewer"}
    )

    assert "no foldable core" in json.loads(result[0].text)["error"]
