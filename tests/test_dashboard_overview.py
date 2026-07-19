"""Tests for the Overview home aggregate (dashboard_overview + /api/overview)."""
from __future__ import annotations

import pytest

from skcapstone import dashboard_overview as do
from skcapstone.card_store import import_from_legacy
from skcapstone.coordination import AgentFile, Board, Task


@pytest.fixture
def home(tmp_path):
    b = Board(tmp_path); b.ensure_dirs()
    b.create_task(Task(id="a1", title="in progress task", created_by="opus"))
    b.save_agent(AgentFile(agent="lumina", current_task="a1", claimed_tasks=["a1"]))
    import_from_legacy(tmp_path)
    return tmp_path


def test_overview_home_shape(home):
    d = do.get_overview_home(home)
    for key in ("agent", "kanban", "active_tasks", "itil", "cmdb", "activity"):
        assert key in d
    assert d["kanban"]["active"] >= 1
    # the in-progress task shows in active work
    assert any(t["id"] == "a1" for t in d["active_tasks"])


def test_overview_route(home):
    from starlette.testclient import TestClient
    from skcapstone.dashboard import create_app
    client = TestClient(create_app(home))
    d = client.get("/api/overview").json()
    assert "kanban" in d and "itil" in d
    # / serves the overview page
    r = client.get("/")
    assert r.status_code == 200 and "SKDashboard" in r.text and "/static/js/overview.js" in r.text
