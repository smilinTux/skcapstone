"""Tests for the Phase 5 assistant console (dashboard_assistant + route)."""
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


def test_most_involved_tasks(home):
    top = da.most_involved_tasks(home, n=2)
    assert top and top[0]["id"] == "t1"        # most events
    assert top[0]["events"] >= 4


def test_build_context_has_sections(home):
    ctx = da.build_context(home)
    assert "KANBAN:" in ctx and "MOST-INVOLVED TASKS:" in ctx and "ITIL KPIs:" in ctx


def test_parse_action():
    txt = 'Sure.\nACTION {"tool": "note", "card_id": "t1", "text": "hi"}'
    a = da._parse_action(txt)
    assert a and a["tool"] == "note" and a["card_id"] == "t1"
    assert da._parse_action("no action here") is None
    # unknown tool rejected
    assert da._parse_action('ACTION {"tool": "delete", "card_id": "t1"}') is None


def test_run_action_gated(home):
    action = {"tool": "note", "card_id": "t1", "text": "from assistant"}
    # without capability -> refused
    r = da._run_action(home, action, "chef", capability_ok=False)
    assert r["ok"] is False and "capability" in r["error"]
    # with capability -> applied, attributed to assistant:<operator>
    r = da._run_action(home, action, "chef", capability_ok=True)
    assert r.get("ok")
    comments = CardStore(home).fold("t1").meta.get("comments", [])
    assert comments[-1]["text"] == "from assistant"
    assert comments[-1]["writer"] == "assistant:chef"


def test_stream_answer_with_stub(home, monkeypatch):
    from skcapstone import skgateway_client as gw

    def fake_stream(messages, **kw):
        yield "Top incident is "
        yield "inc-1.\n"
        yield 'ACTION {"tool": "note", "card_id": "t1", "text": "flagged"}'

    monkeypatch.setattr(gw, "chat_stream", fake_stream)
    frames = list(da.stream_answer(home, "top incidents; note t1", actor="chef", capability_ok=True))
    joined = "".join(frames)
    assert "event: token" in joined and "event: action" in joined and "event: done" in joined
    # the action executed
    assert CardStore(home).fold("t1").meta["comments"][-1]["text"] == "flagged"


def test_assistant_route_streams(home, monkeypatch):
    from starlette.testclient import TestClient
    from skcapstone import skgateway_client as gw
    from skcapstone.dashboard import create_app

    monkeypatch.setattr(gw, "chat_stream", lambda m, **k: iter(["hello ", "world"]))
    client = TestClient(create_app(home))
    r = client.post("/api/assistant", json={"prompt": "hi"})
    assert r.status_code == 200
    assert "event: token" in r.text and "world" in r.text
