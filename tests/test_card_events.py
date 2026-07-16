"""Tests for the kanban overlay events (Phase 3)."""
from __future__ import annotations

from skcapstone.card import (
    CardEvent,
    CardEventLog,
    KanbanBoard,
    fold_overlay,
)
from skcapstone.coordination import Board, Task


# ---- Task 1: append/read ----

def test_card_event_log_append_and_read(tmp_path):
    log = CardEventLog(tmp_path)
    log.append(CardEvent(card_id="x1", action="move", column="review", order=3))
    events = log.read_all()
    assert len(events) == 1
    assert events[0].card_id == "x1"
    assert events[0].column == "review"
    assert events[0].writer  # host stamped on append


# ---- Task 2: fold ----

def test_fold_overlay_move_last_wins_and_labels_accumulate():
    events = [
        CardEvent(card_id="c", action="move", column="ready", order=1,
                  ts="2026-07-16T01:00:00+00:00"),
        CardEvent(card_id="c", action="add_label", label="urgent",
                  ts="2026-07-16T01:01:00+00:00"),
        CardEvent(card_id="c", action="move", column="review", order=2,
                  ts="2026-07-16T01:02:00+00:00"),
        CardEvent(card_id="c", action="link", link_key="pr", link_value="#42",
                  ts="2026-07-16T01:03:00+00:00"),
    ]
    ov = fold_overlay(events)["c"]
    assert ov["column"] == "review"
    assert ov["order"] == 2
    assert "urgent" in ov["labels"]
    assert ov["links"]["pr"] == "#42"


def test_fold_overlay_remove_label():
    events = [
        CardEvent(card_id="c", action="add_label", label="x",
                  ts="2026-07-16T01:00:00+00:00"),
        CardEvent(card_id="c", action="remove_label", label="x",
                  ts="2026-07-16T01:01:00+00:00"),
    ]
    assert "x" not in fold_overlay(events)["c"]["labels"]


# ---- Task 3: apply overlay ----

def test_move_event_overrides_derived_column(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="m1", title="movable", created_by="opus"))  # derived: backlog
    CardEventLog(tmp_path).append(
        CardEvent(card_id="m1", action="move", column="review", order=5)
    )
    kb = KanbanBoard(tmp_path)
    card = next(c for c in kb.cards() if c.id == "m1")
    assert card.status.value == "review"
    assert card.order == 5
    grid = kb.grid()
    assert any(c.id == "m1" for c in grid["feature"]["review"])
    assert all(c.id != "m1" for c in grid["feature"]["backlog"])


def test_bad_column_in_overlay_is_ignored(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="m2", title="x", created_by="o"))
    CardEventLog(tmp_path).append(CardEvent(card_id="m2", action="move", column="bogus"))
    card = next(c for c in KanbanBoard(tmp_path).cards() if c.id == "m2")
    assert card.status.value == "backlog"  # unchanged


# ---- Task 4: WIP ----

def test_wip_report_flags_over_limit(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    log = CardEventLog(tmp_path)
    for i in range(7):
        board.create_task(Task(id=f"w{i}", title=f"t{i}", created_by="o"))
        log.append(CardEvent(card_id=f"w{i}", action="move", column="doing"))
    report = KanbanBoard(tmp_path).wip_report()
    assert report["doing"]["count"] == 7
    assert report["doing"]["limit"] == 6
    assert report["doing"]["over"] is True
    assert report["backlog"]["limit"] is None


def test_wip_excludes_expedite_lane(tmp_path):
    from skcapstone.itil import ITILManager

    board = Board(tmp_path)
    board.ensure_dirs()
    mgr = ITILManager(tmp_path)
    # an incident sits in the expedite lane in 'doing' but must not count toward WIP
    mgr.create_incident(title="down", severity="sev2", created_by="opus")
    report = KanbanBoard(tmp_path).wip_report()
    assert report["doing"]["count"] == 0
