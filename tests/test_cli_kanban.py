"""CLI smoke tests for coord kanban + archive-done (Phases 1-2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from click.testing import CliRunner
import pytest

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
    board.claim_task("opus", "mv1")
    result = CliRunner().invoke(
        main, ["coord", "move", "mv1", "review", "--home", str(tmp_path), "--order", "2"]
    )
    assert result.exit_code == 0, result.output
    card = next(c for c in KanbanBoard(tmp_path).cards() if c.id == "mv1")
    assert card.status.value == "review"
    agent = board.load_agent("opus")
    assert agent is not None
    assert agent.current_task is None
    assert agent.claimed_tasks == ["mv1"]


def test_coord_reconcile_agents_audits_then_repairs_stale_done_claim(tmp_path):
    from skcapstone.card_store import CardStore

    board = Board(tmp_path)
    board.create_task(Task(id="rc1", title="reconcile me", created_by="opus"))
    board.claim_task("opus", "rc1")
    CardStore(tmp_path).append_event("rc1", "move", "operator", column="done")

    audit = CliRunner().invoke(main, ["coord", "reconcile-agents", "--home", str(tmp_path)])
    assert audit.exit_code != 0
    assert "done_still_claimed" in audit.output

    repaired = CliRunner().invoke(
        main,
        ["coord", "reconcile-agents", "--home", str(tmp_path), "--repair"],
    )
    assert repaired.exit_code == 0, repaired.output
    assert '"clean": true' in repaired.output


def test_coord_move_rejects_bad_column(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="mv2", title="x", created_by="o"))
    result = CliRunner().invoke(
        main, ["coord", "move", "mv2", "nonsense", "--home", str(tmp_path)]
    )
    assert result.exit_code != 0


def test_coord_move_missing_task_is_a_normalized_cli_error(tmp_path):
    result = CliRunner().invoke(
        main, ["coord", "move", "missing", "review", "--home", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert "Error: Task missing not found" in result.output
    assert result.exception is not None
    assert result.exception.__class__.__name__ == "SystemExit"


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


def test_coord_maintain_runs_both_sweeps(tmp_path):
    from datetime import datetime, timedelta, timezone

    board = Board(tmp_path)
    board.ensure_dirs()
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    board.create_task(Task(id="mt1", title="old done", created_by="o", created_at=old))
    board.save_agent(AgentFile(agent="o", completed_tasks=["mt1"]))
    board.create_task(Task(id="mt2", title="old open", created_by="o", created_at=old))
    result = CliRunner().invoke(main, ["coord", "maintain", "--home", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "mt1" in board.archived_ids()  # done archived
    assert "mt2" in board.archived_ids()  # stale-open archived


def test_coord_kanban_json_uses_folded_dependencies(tmp_path):
    """Kanban must render the authoritative dependency amendment fold."""
    import json

    from skcapstone.card_store import CardCore, CardStore

    store = CardStore(tmp_path)
    store.create(CardCore(id="gate0002", title="Incomplete gate"))
    store.create(CardCore(id="target02", title="Folded dependency target"))
    store.append_event(
        "target02",
        "add_dependency",
        "governance",
        dependency="gate0002",
        reason="test gate",
    )

    result = CliRunner().invoke(main, ["coord", "kanban", "--home", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    grid = json.loads(result.output)
    cards = [
        card
        for columns in grid.values()
        for column_cards in columns.values()
        for card in column_cards
    ]
    target = next(card for card in cards if card["id"] == "target02")
    assert target["dependencies"] == ["gate0002"]
    with pytest.raises(ValueError, match="incomplete dependencies: gate0002"):
        Board(tmp_path).claim_task("worker", "target02")
