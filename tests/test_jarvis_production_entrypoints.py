"""Jarvis cannot bypass Casey authorization through real card surfaces."""

import asyncio

from click.testing import CliRunner

from skcapstone import sdk
from skcapstone.cli import main
from skcapstone.mcp_server import call_tool


def test_cli_claim_fails_before_board_mutation_for_unsigned_jarvis(tmp_path) -> None:
    result = CliRunner().invoke(
        main,
        ["coord", "claim", "deadbeef", "--home", str(tmp_path), "--agent", "jarvis"],
    )

    assert result.exit_code != 0
    assert "--casey-authorization" in str(result.exception)


def test_mcp_complete_fails_before_board_mutation_for_unsigned_jarvis(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    response = asyncio.run(
        call_tool("coord_complete", {"task_id": "deadbeef", "agent_name": "jarvis"})
    )

    assert "casey-authorization" in response[0].text


def test_cli_move_fails_before_board_mutation_for_unsigned_jarvis(tmp_path) -> None:
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
    assert "--casey-authorization" in str(result.exception)


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


def test_mcp_move_fails_before_board_mutation_for_unsigned_jarvis(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    response = asyncio.run(
        call_tool(
            "coord_move",
            {"task_id": "deadbeef", "column": "doing", "agent": "jarvis"},
        )
    )

    assert "casey-authorization" in response[0].text


def test_mcp_move_does_not_gate_non_jarvis_actor(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    response = asyncio.run(
        call_tool(
            "coord_move",
            {"task_id": "deadbeef", "column": "doing", "agent": "link"},
        )
    )

    assert "casey-authorization" not in response[0].text


def test_sdk_create_fails_before_board_mutation_for_unsigned_jarvis(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sdk, "_shared_home", lambda: tmp_path)

    try:
        sdk.coord_create("must not exist", created_by="jarvis")
    except ValueError as exc:
        assert "--casey-authorization" in str(exc)
    else:
        raise AssertionError("unsigned Jarvis SDK mutation was accepted")
