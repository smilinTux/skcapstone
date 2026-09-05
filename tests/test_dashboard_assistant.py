"""Tests for dashboard assistant (AssistantScope boundary via skcapstone shim)."""

from __future__ import annotations

import json

import pytest
from skdashboard.assistant_client import AssistantScope

from skcapstone import dashboard_assistant as da


def _scope(**overrides) -> AssistantScope:
    """Build a minimal authorized AssistantScope for tests."""
    data = {
        "tenant_id": "platform",
        "matter_id": None,
        "classification": "internal",
        "source_rights": ("skdashboard",),
        "egress_profile": "local-only",
        "read_authorized": True,
    }
    data.update(overrides)
    return AssistantScope(**data)


def test_build_context_returns_scope_metadata(tmp_path):
    context = json.loads(da.build_context(tmp_path, scope=_scope(matter_id="m1")))
    assert context == {
        "classification": "internal",
        "matter_id": "m1",
        "source_rights": ["skdashboard"],
        "tenant_id": "platform",
    }


def test_build_context_rejects_missing_scope(tmp_path):
    with pytest.raises(PermissionError, match="authorized assistant scope required"):
        da.build_context(tmp_path)


def test_stream_answer_with_stub(tmp_path, monkeypatch):
    class StubClient:
        def chat_stream(self, messages, **kw):
            yield "ok"

    monkeypatch.setattr(da, "get_client", lambda: StubClient())
    frames = list(da.stream_answer(tmp_path, "hi", actor="chef", scope=_scope()))
    joined = "".join(frames)
    assert "event: token" in joined and "event: done" in joined
    assert "event: action" not in joined


def test_stream_answer_requires_scope(tmp_path, monkeypatch):
    called = {"n": 0}

    def boom():
        called["n"] += 1
        raise AssertionError("get_client must not run without scope")

    monkeypatch.setattr(da, "get_client", boom)
    frames = list(da.stream_answer(tmp_path, "hi"))
    assert called["n"] == 0
    assert "authorized_scope_required" in frames[0]
    assert "event: done" in frames[-1]


def test_legacy_mutating_helpers_are_not_exposed():
    assert not hasattr(da, "board_summary")
    assert not hasattr(da, "most_involved_tasks")
    assert not hasattr(da, "_parse_action")
    assert not hasattr(da, "_run_action")


def test_assistant_route_passes_scope(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from skcapstone.dashboard import create_app

    calls = {}

    def fake_stream(home, prompt, actor="operator", capability_ok=False, scope=None):
        calls["scope"] = scope
        calls["prompt"] = prompt
        yield "event: done\ndata: {}\n\n"

    monkeypatch.setattr("skdashboard.dashboard_assistant.stream_answer", fake_stream)
    r = TestClient(create_app(tmp_path)).post(
        "/api/assistant", json={"prompt": "hello"}, headers={"x-sk-actor": "operator"}
    )
    assert r.status_code == 200
    assert calls["prompt"] == "hello"
    assert calls["scope"].read_authorized is True
    assert calls["scope"].tenant_id == "platform"
    assert calls["scope"].source_rights == ("skdashboard",)
