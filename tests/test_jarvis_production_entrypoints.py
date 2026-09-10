"""Jarvis can coordinate directly while external effects remain gated."""

import asyncio

from click.testing import CliRunner

from skcapstone import sdk
from skcapstone.cli import main
from skcapstone.mcp_server import call_tool


def test_cli_claim_reaches_board_without_signed_jarvis_artifact(tmp_path) -> None:
    result = CliRunner().invoke(
        main,
        ["coord", "claim", "deadbeef", "--home", str(tmp_path), "--agent", "jarvis"],
    )

    assert result.exit_code != 0
    assert "casey-authorization" not in str(result.exception)


def test_mcp_complete_reaches_board_without_signed_jarvis_artifact(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    response = asyncio.run(
        call_tool("coord_complete", {"task_id": "deadbeef", "agent_name": "jarvis"})
    )

    assert "casey-authorization" not in response[0].text


def test_cli_move_reaches_board_without_signed_jarvis_artifact(tmp_path) -> None:
    result = CliRunner().invoke(
        main,
        [
            "coord",
            "move",
            "deadbeef",
            "doing",
            "--home",
            str(tmp_path),
            "--agent",
            "jarvis",
        ],
    )

    assert result.exit_code != 0
    assert "casey-authorization" not in str(result.exception)


def test_cli_move_does_not_gate_non_jarvis_actor(tmp_path) -> None:
    result = CliRunner().invoke(
        main,
        [
            "coord",
            "move",
            "deadbeef",
            "doing",
            "--home",
            str(tmp_path),
            "--agent",
            "link",
        ],
    )

    assert result.exit_code != 0
    assert "casey-authorization" not in str(result.exception)


def test_mcp_move_reaches_board_without_signed_jarvis_artifact(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    response = asyncio.run(
        call_tool(
            "coord_move",
            {"task_id": "deadbeef", "column": "doing", "agent": "jarvis"},
        )
    )

    assert "casey-authorization" not in response[0].text


def test_mcp_move_does_not_gate_non_jarvis_actor(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    response = asyncio.run(
        call_tool(
            "coord_move",
            {"task_id": "deadbeef", "column": "doing", "agent": "link"},
        )
    )

    assert "casey-authorization" not in response[0].text


def test_sdk_create_allows_unsigned_jarvis_coordination(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sdk, "_shared_home", lambda: tmp_path)

    created = sdk.coord_create("directed repair", created_by="jarvis")

    assert isinstance(created, str)
    assert created
