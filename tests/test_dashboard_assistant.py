"""Compatibility tests for the extracted read-only dashboard assistant."""

from __future__ import annotations

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


def test_build_context_has_sections(home):
    ctx = da.build_context(home)
    aggregate = __import__("json").loads(ctx)
    assert set(aggregate) == {"itil", "kanban"}
    assert aggregate["kanban"]["active"] >= 2


def test_assistant_has_no_action_surface():
    for removed in ("most_involved_tasks", "_parse_action", "_run_action"):
        assert not hasattr(da, removed)


def test_stream_answer_with_stub(home, monkeypatch):
    class Client:
        def chat_stream(self, messages, *, actor):
            assert actor == "chef"
            assert messages[1]["content"].startswith("AGGREGATE SNAPSHOT:")
            yield "Top incident count is one."

    monkeypatch.setattr("skdashboard.assistant_client.get_client", lambda: Client())
    frames = list(da.stream_answer(home, "top incident count", actor="chef"))
    joined = "".join(frames)
    assert "event: token" in joined and "event: done" in joined
    assert "event: action" not in joined


def test_assistant_route_streams(home, monkeypatch):
    from starlette.testclient import TestClient

    from skcapstone import skgateway_client as gw
    from skcapstone.dashboard import create_app

    monkeypatch.setattr(gw, "chat_stream", lambda m, **k: iter(["hello ", "world"]))
    client = TestClient(create_app(home))
    r = client.post("/api/assistant", json={"prompt": "hi"})
    assert r.status_code == 200
    assert "event: token" in r.text and "world" in r.text
