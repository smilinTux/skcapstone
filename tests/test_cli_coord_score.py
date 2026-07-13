"""Tests for the `coord score` CLI command."""

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


def test_coord_score_records_grade(tmp_path: Path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="abc12345", title="CLI score"))
    result = CliRunner().invoke(
        _main(),
        ["coord", "score", "abc12345", "--home", str(tmp_path),
         "--round", "1", "--score", "5", "--notes", "clean",
         "--harness", "claude_code", "--phase", "grade",
         "--ref", "https://gh/pr/9"],
    )
    assert result.exit_code == 0, result.output
    t = {x.id: x for x in board.load_tasks()}["abc12345"]
    ap = t.meta["autopilot"]
    assert ap["scores"][0]["score"] == 5
    assert ap["scores"][0]["harness"] == "claude_code"
    assert ap["phase"] == "grade"
    assert ap["pr"] == "https://gh/pr/9"


def test_coord_score_missing_task_errors(tmp_path: Path):
    board = Board(tmp_path)
    board.ensure_dirs()
    result = CliRunner().invoke(
        _main(),
        ["coord", "score", "deadbeef", "--home", str(tmp_path),
         "--round", "1", "--score", "3"],
    )
    assert result.exit_code == 1
    assert "Error" in result.output or "error" in result.output.lower()
