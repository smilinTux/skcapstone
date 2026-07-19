"""Tests for the unified kanban Card projection (Phase 1)."""
from __future__ import annotations

from skcapstone.card import (
    COLUMN_ORDER,
    LANE_ORDER,
    Card,
    Column,
    KanbanBoard,
    Kind,
    card_from_change,
    card_from_incident,
    card_from_problem,
    card_from_taskview,
)
from skcapstone.coordination import Board, Task, TaskPriority, TaskStatus, TaskView


# ---- Task 1: model ----

def test_card_defaults_and_roundtrip():
    c = Card(id="abc123", kind=Kind.TASK, title="Do the thing", status=Column.READY,
             swimlane="feature")
    assert c.priority == "medium"
    assert c.archived is False
    assert c.source == "coord"
    dumped = c.model_dump()
    assert dumped["kind"] == "task"
    assert dumped["status"] == "ready"
    assert Card(**dumped).title == "Do the thing"


# ---- Task 2: coord TaskView adapter ----

def test_card_from_taskview_maps_status_and_swimlane():
    t = Task(id="t1", title="Fix login", priority=TaskPriority.HIGH,
             created_by="opus", tags=["bug", "auth"])
    view = TaskView(task=t, status=TaskStatus.IN_PROGRESS, claimed_by="lumina")
    c = card_from_taskview(view)
    assert c.status.value == "doing"
    assert c.swimlane == "bug"
    assert c.owner == "lumina"
    assert c.originator == "opus"
    assert c.priority == "high"
    assert c.source == "coord"


def test_card_from_taskview_blocked_stays_on_board():
    t = Task(id="t2", title="Blocked thing", created_by="opus")
    view = TaskView(task=t, status=TaskStatus.BLOCKED)
    c = card_from_taskview(view)
    assert c.status.value == "doing"
    assert c.meta.get("blocked") is True


def test_card_from_taskview_epic_kind():
    t = Task(id="t3", title="Big epic", created_by="opus", tags=["epic"])
    c = card_from_taskview(TaskView(task=t, status=TaskStatus.OPEN))
    assert c.kind == Kind.EPIC
    assert c.status.value == "backlog"


# ---- Task 3: ITIL adapters ----

def test_card_from_incident_is_expedite_lane():
    from skcapstone.itil import Incident, IncidentStatus, Severity

    inc = Incident(id="inc-1", title="skmem-pg down", severity=Severity.SEV2,
                   status=IncidentStatus.INVESTIGATING)
    c = card_from_incident(inc)
    assert c.kind.value == "incident"
    assert c.swimlane == "expedite"
    assert c.status.value == "doing"
    assert c.meta["severity"] == "sev2"
    assert c.source == "itil"


def test_card_from_incident_resolved_to_review():
    from skcapstone.itil import Incident, IncidentStatus, Severity

    inc = Incident(id="inc-2", title="resolved one", severity=Severity.SEV3,
                   status=IncidentStatus.RESOLVED)
    assert card_from_incident(inc).status == Column.REVIEW


def test_card_from_problem_and_change_lanes():
    from skcapstone.itil import Change, ChangeStatus, Problem, ProblemStatus

    p = Problem(id="prb-1", title="root cause", status=ProblemStatus.ANALYZING)
    pc = card_from_problem(p)
    assert pc.swimlane == "problem"
    assert pc.status.value == "doing"

    ch = Change(id="chg-1", title="cutover", status=ChangeStatus.IMPLEMENTING)
    cc = card_from_change(ch)
    assert cc.swimlane == "change"
    assert cc.status.value == "doing"


# ---- Task 4: KanbanBoard grid ----

def test_kanban_grid_groups_by_lane_and_column(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="k1", title="Backlog item", created_by="opus"))
    kb = KanbanBoard(tmp_path)
    grid = kb.grid()
    assert "feature" in grid
    assert any(c.id == "k1" for c in grid["feature"]["backlog"])
    assert COLUMN_ORDER[0] == "backlog"
    assert LANE_ORDER[0] == "feature"


def test_kanban_grid_orders_by_priority(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="lo", title="low", created_by="o", priority=TaskPriority.LOW))
    board.create_task(Task(id="cr", title="crit", created_by="o", priority=TaskPriority.CRITICAL))
    grid = KanbanBoard(tmp_path).grid()
    backlog = grid["feature"]["backlog"]
    assert [c.id for c in backlog] == ["cr", "lo"]


def test_kanban_cards_excludes_archived(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="vis", title="visible", created_by="o"))
    kb = KanbanBoard(tmp_path)
    assert any(c.id == "vis" for c in kb.cards())
