"""Tests for coord archival + aging (Phase 2)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from skcapstone.coordination import AgentFile, Board, Task


def test_archive_task_hides_from_default_views(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="a1", title="Old done thing", created_by="opus"))
    board.archive_task("a1", by="opus")
    assert "a1" in board.archived_ids()
    assert all(v.task.id != "a1" for v in board.get_task_views())
    assert any(v.task.id == "a1" for v in board.get_task_views(include_archived=True))


def test_archive_done_tasks_ages_off_old_done(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    board.create_task(Task(id="d1", title="Old done", created_by="opus", created_at=old))
    board.save_agent(AgentFile(agent="opus", completed_tasks=["d1"]))
    archived = board.archive_done_tasks(older_than_days=14)
    assert "d1" in archived
    assert "d1" in board.archived_ids()


def test_archive_done_tasks_keeps_recent(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="d2", title="Fresh done", created_by="opus"))
    board.save_agent(AgentFile(agent="opus", completed_tasks=["d2"]))
    assert board.archive_done_tasks(older_than_days=14) == []


def test_archive_done_tasks_ignores_open(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    board.create_task(Task(id="o1", title="Old open", created_by="opus", created_at=old))
    # not completed by any agent -> still open, must not be archived
    assert board.archive_done_tasks(older_than_days=14) == []


def test_archive_done_tasks_dry_run_writes_nothing(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    board.create_task(Task(id="d3", title="Old done", created_by="opus", created_at=old))
    board.save_agent(AgentFile(agent="opus", completed_tasks=["d3"]))
    would = board.archive_done_tasks(older_than_days=14, dry_run=True)
    assert would == ["d3"]
    assert "d3" not in board.archived_ids()


def test_age_stale_open_archives_ancient_open(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    ancient = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    board.create_task(Task(id="s1", title="ancient open", created_by="o", created_at=ancient))
    board.create_task(Task(id="s2", title="fresh open", created_by="o"))
    aged = board.age_stale_open(older_than_days=90)
    assert aged == ["s1"]
    assert "s1" in board.archived_ids()
    assert "s2" not in board.archived_ids()


def test_age_stale_open_ignores_claimed(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    ancient = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    board.create_task(Task(id="s3", title="ancient claimed", created_by="o", created_at=ancient))
    board.save_agent(AgentFile(agent="opus", claimed_tasks=["s3"]))
    assert board.age_stale_open(older_than_days=90) == []
