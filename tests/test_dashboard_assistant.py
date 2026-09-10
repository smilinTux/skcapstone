"""Tests for the Phase 5 assistant console (dashboard_assistant + route)."""

from __future__ import annotations

import json

import pytest
from skdashboard.assistant_client import AssistantScope

from skcapstone import dashboard_assistant as da
from skcapstone.card_store import CardStore, import_from_legacy
from skcapstone.coordination import Board, Task


def _authorized_scope() -> AssistantScope:
    """Minimal policy scope that unlocks the read-only assistant boundary."""
    return AssistantScope(
        tenant_id="platform",
        matter_id=None,
        classification="internal",
        source_rights=("skdashboard",),
        egress_profile="local",
        read_authorized=True,
    )


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


def test_legacy_board_summary_is_not_exposed():
    """Kanban board_summary left the assistant surface; only scoped context remains."""
    assert not hasattr(da, "board_summary")


def test_legacy_mutating_helpers_are_not_exposed():
    assert not hasattr(da, "most_involved_tasks")
    assert not hasattr(da, "_parse_action")
    assert not hasattr(da, "_run_action")


def test_build_context_requires_authorized_scope(home):
    with pytest.raises(PermissionError, match="authorized assistant scope required"):
        da.build_context(home)


def test_build_context_has_authorized_scope_fields(home):
    scope = _authorized_scope()
    context = json.loads(da.build_context(home, scope))
    assert set(context) == {
        "tenant_id",
        "matter_id",
        "classification",
        "source_rights",
    }
    assert context["tenant_id"] == "platform"
    assert context["matter_id"] is None
    assert context["classification"] == "internal"
    assert context["source_rights"] == ["skdashboard"]
    assert "t1" not in json.dumps(context)
    assert "Busy task" not in json.dumps(context)


def test_stream_answer_with_stub(home, monkeypatch):
    from skdashboard import assistant_client

    before = CardStore(home).fold("t1").meta.get("comments")

    class StubClient:
        def chat_stream(self, messages, **kw):
            yield "Top incident is inc-1."

    # dashboard_assistant binds get_client at import time; patch the bound name.
    monkeypatch.setattr(da, "get_client", lambda: StubClient())
    monkeypatch.setattr(assistant_client, "get_client", lambda: StubClient())
    frames = list(
        da.stream_answer(
            home,
            "top incidents",
            actor="chef",
            scope=_authorized_scope(),
        )
    )
    joined = "".join(frames)
    assert "event: token" in joined and "event: done" in joined
    assert "event: action" not in joined
    assert CardStore(home).fold("t1").meta.get("comments") == before


def test_assistant_route_streams(home, monkeypatch):
    import skdashboard.dashboard_assistant as skda
    from starlette.testclient import TestClient

    from skcapstone.dashboard import create_app

    def _fake_stream(home_path, prompt, actor="operator", capability_ok=False, scope=None):
        assert scope is not None and scope.read_authorized is True
        yield 'event: token\ndata: {"text": "hello "}\n\n'
        yield 'event: token\ndata: {"text": "world"}\n\n'
        yield "event: done\ndata: {}\n\n"

    # create_app imports stream_answer from skdashboard.dashboard_assistant.
    monkeypatch.setattr(skda, "stream_answer", _fake_stream)
    monkeypatch.setattr(da, "stream_answer", _fake_stream)
    client = TestClient(create_app(home))
    r = client.post(
        "/api/assistant",
        json={"prompt": "hi"},
        headers={"x-sk-actor": "operator"},
    )
    assert r.status_code == 200
    assert "event: token" in r.text and "world" in r.text
