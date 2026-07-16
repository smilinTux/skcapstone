"""CLI smoke tests for coord kanban + archive-done (Phases 1-2)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from click.testing import CliRunner

from skcapstone.cli import main
from skcapstone.coordination import AgentFile, Board, Task


def test_coord_kanban_html_written(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="c1", title="CLI card", created_by="opus"))
    out = tmp_path / "board.html"
    result = CliRunner().invoke(
        main, ["coord", "kanban", "--home", str(tmp_path), "--html", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "CLI card" in out.read_text()


def test_coord_kanban_text_summary(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="c2", title="Summary card", created_by="opus"))
    result = CliRunner().invoke(main, ["coord", "kanban", "--home", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "feature" in result.output.lower()


def test_coord_archive_done_dry_run(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    board.create_task(Task(id="cd1", title="old", created_by="opus", created_at=old))
    board.save_agent(AgentFile(agent="opus", completed_tasks=["cd1"]))
    result = CliRunner().invoke(
        main, ["coord", "archive-done", "--home", str(tmp_path), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "1" in result.output
    assert "cd1" not in board.archived_ids()  # dry-run does not write


def test_coord_move_appends_event(tmp_path):
    from skcapstone.card import KanbanBoard
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="mv1", title="move me", created_by="opus"))
    result = CliRunner().invoke(
        main, ["coord", "move", "mv1", "review", "--home", str(tmp_path), "--order", "2"]
    )
    assert result.exit_code == 0, result.output
    card = next(c for c in KanbanBoard(tmp_path).cards() if c.id == "mv1")
    assert card.status.value == "review"


def test_coord_move_rejects_bad_column(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="mv2", title="x", created_by="o"))
    result = CliRunner().invoke(main, ["coord", "move", "mv2", "nonsense", "--home", str(tmp_path)])
    assert result.exit_code != 0


def test_coord_age_backlog_dry_run(tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="ab1", title="ancient open", created_by="o", created_at=old))
    result = CliRunner().invoke(
        main, ["coord", "age-backlog", "--home", str(tmp_path), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "1" in result.output
    assert "ab1" not in board.archived_ids()
