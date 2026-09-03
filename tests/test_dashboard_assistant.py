"""Tests for the Phase 5 assistant console (dashboard_assistant + route)."""

from __future__ import annotations

import json

import pytest

from skcapstone import dashboard_assistant as da
from skcapstone.card_store import CardStore, import_from_legacy
from skcapstone.coordination import Board, Task


@pytest.fixture
def home(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="t1", title="Busy task", created_by="opus"))
    board.create_task(Task(id="t2", title="Quiet task", created_by="opus"))
    import_from_legacy(tmp_path)
    # make t1 the most-involved by adding events
    for i in range(4):
        CardStore(tmp_path).append_event("t1", "note", "opus", text=f"n{i}")
    return tmp_path


def test_board_summary(home):
    s = da.board_summary(home)
    assert s["active"] >= 2 and "by_column" in s and "wip" in s


def test_legacy_mutating_helpers_are_not_exposed():
    assert not hasattr(da, "most_involved_tasks")
    assert not hasattr(da, "_parse_action")
    assert not hasattr(da, "_run_action")


def test_build_context_has_sections(home):
    context = json.loads(da.build_context(home))
    assert set(context) == {"itil", "kanban"}
    assert set(context["kanban"]) == {"active", "by_column", "by_lane", "wip"}
    assert "t1" not in json.dumps(context)
    assert "Busy task" not in json.dumps(context)


def test_stream_answer_with_stub(home, monkeypatch):
    from skdashboard import assistant_client

    before = CardStore(home).fold("t1").meta.get("comments")

    class StubClient:
        def chat_stream(self, messages, **kw):
            yield "Top incident is inc-1."

    monkeypatch.setattr(assistant_client, "get_client", lambda: StubClient())
    frames = list(da.stream_answer(home, "top incidents", actor="chef"))
    joined = "".join(frames)
    assert "event: token" in joined and "event: done" in joined
    assert "event: action" not in joined
    assert CardStore(home).fold("t1").meta.get("comments") == before


def test_assistant_route_streams(home, monkeypatch):
    from starlette.testclient import TestClient

    from skcapstone import skgateway_client as gw
    from skcapstone.dashboard import create_app

    monkeypatch.setattr(gw, "chat_stream", lambda m, **k: iter(["hello ", "world"]))
    client = TestClient(create_app(home))
    r = client.post("/api/assistant", json={"prompt": "hi"})
    assert r.status_code == 200
    assert "event: token" in r.text and "world" in r.text
