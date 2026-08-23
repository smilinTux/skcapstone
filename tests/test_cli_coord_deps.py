"""Tests for dependency enforcement on `coord claim` and the blocked/unblocked
distinction in `coord status` (card 34be7725)."""

from pathlib import Path

import click
from click.testing import CliRunner

from skcapstone.cli.coord import register_coord_commands
from skcapstone.coordination import Board, Task


def _main() -> click.Group:
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _chain_board(tmp_path: Path) -> Board:
    """Board with dep -> child (child depends on dep, which is not done)."""
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="aa000001", title="root dependency"))
    board.create_task(Task(id="bb000002", title="child task", dependencies=["aa000001"]))
    board.create_task(Task(id="cc000003", title="no deps"))
    return board


def test_claim_blocked_task_fails(tmp_path: Path):
    """Claiming a card with an incomplete dependency exits non-zero."""
    _chain_board(tmp_path)
    result = CliRunner().invoke(
        _main(),
        ["coord", "claim", "bb000002", "--home", str(tmp_path), "--agent", "opus"],
    )
    assert result.exit_code == 1
    assert "incomplete dependencies" in result.output.lower()
    assert "aa000001" in result.output
    board = Board(tmp_path)
    agent = board.load_agent("opus")
    assert agent is None or "bb000002" not in agent.claimed_tasks


def test_claim_blocked_task_with_force_succeeds(tmp_path: Path):
    """--force overrides the dependency gate."""
    _chain_board(tmp_path)
    result = CliRunner().invoke(
        _main(),
        ["coord", "claim", "bb000002", "--home", str(tmp_path), "--agent", "opus", "--force"],
    )
    assert result.exit_code == 0, result.output
    board = Board(tmp_path)
    assert "bb000002" in board.load_agent("opus").claimed_tasks


def test_claim_unblocked_after_dependency_done(tmp_path: Path):
    """Once the dependency is done, the child claims without --force."""
    board = _chain_board(tmp_path)
    board.claim_task("jarvis", "aa000001")
    board.complete_task("jarvis", "aa000001")
    result = CliRunner().invoke(
        _main(),
        ["coord", "claim", "bb000002", "--home", str(tmp_path), "--agent", "opus"],
    )
    assert result.exit_code == 0, result.output


def test_status_distinguishes_blocked_from_open(tmp_path: Path):
    """`coord status` labels dep-blocked cards BLOCKED and free cards OPEN."""
    _chain_board(tmp_path)
    result = CliRunner().invoke(
        _main(),
        ["coord", "status", "--home", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "BLOCKED" in result.output  # bb000002 (aa000001 not done)
    assert "OPEN" in result.output  # aa000001 and cc000003 remain plainly open


def test_status_filter_blocked(tmp_path: Path):
    """`coord status --status blocked` lists only dep-blocked cards."""
    _chain_board(tmp_path)
    result = CliRunner().invoke(
        _main(),
        ["coord", "status", "--home", str(tmp_path), "--status", "blocked"],
    )
    assert result.exit_code == 0, result.output
    assert "bb000002" in result.output
    assert "aa000001" not in result.output
    assert "cc000003" not in result.output
