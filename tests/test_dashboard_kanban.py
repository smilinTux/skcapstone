"""Tests for the Phase 2 interactive kanban API (dashboard_kanban + routes)."""
from __future__ import annotations

import json

import pytest

from skcapstone.card_store import CardStore, import_from_legacy
from skcapstone.coordination import Board, Task, TaskPriority
from skcapstone.dashboard import create_app
from skcapstone import dashboard_kanban as dk


@pytest.fixture
def home(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="t1", title="Open feature", created_by="opus", tags=["bug"]))
    board.create_task(Task(id="t2", title="High one", created_by="opus", priority=TaskPriority.HIGH))
    import_from_legacy(tmp_path)
    return tmp_path


def test_get_kanban_shape(home):
    data = dk.get_kanban(home)
    assert "columns" in data and data["columns"][0] == "backlog"
    assert "lanes" in data and any(l["key"] == "feature" for l in data["lanes"])
    assert "wip" in data and "doing" in data["wip"]


def test_get_card_returns_card_and_activity(home):
    data = dk.get_card(home, "t1")
    assert data["card"]["id"] == "t1"
    assert isinstance(data["activity"], list)
    assert data["activity"]  # import wrote at least a move event


def test_get_card_missing(home):
    assert "error" in dk.get_card(home, "nope")


def test_apply_move(home):
    r = dk.apply_mutation(home, "t1", "move", "operator", column="review")
    assert r["ok"] and r["card"]["status"] == "review"
    assert CardStore(home).fold("t1").status.value == "review"


def test_apply_move_bad_column(home):
    assert "error" in dk.apply_mutation(home, "t1", "move", "op", column="bogus")


def test_apply_assign_and_priority_and_label_and_note(home):
    assert dk.apply_mutation(home, "t2", "assign", "op", owner="lumina")["card"]["owner"] == "lumina"
    assert dk.apply_mutation(home, "t2", "priority", "op", priority="critical")["card"]["priority"] == "critical"
    assert "urgent" in dk.apply_mutation(home, "t2", "add_label", "op", label="urgent")["card"]["labels"]
    r = dk.apply_mutation(home, "t2", "note", "op", text="looking into it")
    comments = r["card"]["meta"].get("comments", [])
    assert comments and comments[-1]["text"] == "looking into it"


def test_apply_unknown_action(home):
    assert "error" in dk.apply_mutation(home, "t1", "frobnicate", "op")


# ---- HTTP routes via TestClient ----

def test_routes_kanban_card_and_mutation(home):
    from starlette.testclient import TestClient
    client = TestClient(create_app(home))

    k = client.get("/api/kanban")
    assert k.status_code == 200 and "lanes" in k.json()

    c = client.get("/api/card/t1")
    assert c.status_code == 200 and c.json()["card"]["id"] == "t1"

    m = client.post("/api/card/t1/move", json={"column": "doing"}, headers={"X-SK-Actor": "tester"})
    assert m.status_code == 200 and m.json()["card"]["status"] == "doing"
    # the mutation was attributed to the actor
    ev = client.get("/api/card/t1").json()["activity"]
    assert any(e.get("action") == "move" and e.get("writer") == "tester" for e in ev)


def test_board_page_served(home):
    from starlette.testclient import TestClient
    client = TestClient(create_app(home))
    r = client.get("/board")
    assert r.status_code == 200
    assert "SKDashboard" in r.text
    assert "/static/js/board.js" in r.text
    # static assets are mounted
    assert client.get("/static/vendor/Sortable.min.js").status_code == 200
    assert client.get("/static/css/board.css").status_code == 200
