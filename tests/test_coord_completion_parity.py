"""CLI and MCP must share the governed review completion mutation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from skcapstone.cli.coord import register_coord_commands
from skcapstone.coordination import Board, Task
from skcapstone.mcp_server import call_tool

_REQUIRED = (
    "ci_check_docs",
    "ci_check_gitleaks",
    "ci_check_lint",
    "ci_check_shim_imports",
    "ci_check_python311",
    "ci_check_python312",
)


def _main() -> click.Group:
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _review_home(tmp_path: Path, title: str, state: str | None) -> Path:
    home = tmp_path / "home"
    board = Board(home)
    board.ensure_dirs()
    board.create_task(Task(id="abcd1234", title="completion parity fixture"))
    board.claim_task("reviewer", "abcd1234")
    core = home / "cards" / "abcd1234" / "core.json"
    payload = json.loads(core.read_text())
    payload["title"] = title
    core.write_text(json.dumps(payload))
    rows = [
        {
            "card_id": "abcd1234",
            "action": "link",
            "link_key": "verdict",
            "link_value": "PASS",
            "ts": "2026-09-09T08:00:00Z",
        }
    ]
    for key in _REQUIRED:
        if state is not None:
            rows.append(
                {
                    "card_id": "abcd1234",
                    "action": "link",
                    "link_key": key,
                    "link_value": state,
                    "ts": "2026-09-09T08:01:00Z",
                }
            )
    events = home / "coordination" / "card_events"
    events.mkdir(parents=True, exist_ok=True)
    (events / "test.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    return home


def _cli_complete(home: Path) -> tuple[bool, str]:
    result = CliRunner().invoke(
        _main(),
        ["coord", "complete", "abcd1234", "--home", str(home), "--agent", "reviewer"],
    )
    return result.exit_code == 0, result.output


def _cli_move(home: Path) -> tuple[bool, str]:
    result = CliRunner().invoke(
        _main(),
        ["coord", "move", "abcd1234", "done", "--home", str(home), "--agent", "reviewer"],
    )
    return result.exit_code == 0, result.output


async def _mcp_complete(home: Path) -> tuple[bool, str]:
    with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(home)):
        result = await call_tool(
            "coord_complete", {"task_id": "abcd1234", "agent_name": "reviewer"}
        )
    payload = json.loads(result[0].text)
    return "error" not in payload, str(payload)


async def _mcp_move(home: Path) -> tuple[bool, str]:
    with patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(home)):
        result = await call_tool(
            "coord_move",
            {"task_id": "abcd1234", "column": "done", "agent": "reviewer"},
        )
    payload = json.loads(result[0].text)
    return "error" not in payload, str(payload)


async def _invoke(home: Path, entrypoint: str) -> tuple[bool, str]:
    if entrypoint == "cli_complete":
        return _cli_complete(home)
    if entrypoint == "cli_move":
        return _cli_move(home)
    if entrypoint == "mcp_complete":
        return await _mcp_complete(home)
    return await _mcp_move(home)


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["[X][REVIEW] review", "[X][REREVIEW] rereview"])
@pytest.mark.parametrize("entrypoint", ["cli_complete", "cli_move", "mcp_complete", "mcp_move"])
async def test_cli_mcp_accept_only_complete_exact_success(
    tmp_path: Path, title: str, entrypoint: str
) -> None:
    home = _review_home(tmp_path, title, "SUCCESS")
    ok, detail = await _invoke(home, entrypoint)
    assert ok, detail


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["[X][REVIEW] review", "[X][REREVIEW] rereview"])
@pytest.mark.parametrize("entrypoint", ["cli_complete", "cli_move", "mcp_complete", "mcp_move"])
@pytest.mark.parametrize(
    "state",
    [
        "PASS",
        "PASSED",
        "SUCCESSFUL",
        "SUCCESS extra",
        "success",
        " SUCCESS ",
        None,
        "UNKNOWN",
        "PENDING",
    ],
)
async def test_cli_mcp_fail_closed_for_noncanonical_or_incomplete_ci(
    tmp_path: Path, title: str, entrypoint: str, state: str | None
) -> None:
    home = _review_home(tmp_path, title, state)
    ok, detail = await _invoke(home, entrypoint)
    assert not ok, detail
    assert Board(home).load_agent("reviewer").current_task == "abcd1234"
