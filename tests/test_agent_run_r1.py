"""R1 (card 182b947f): execute-mode must route through a sandboxed, graded
executor (skharness.autocode), never raw ``claude -p``.

The safety property proved here: even with SKAI_RUNNER_LIVE=1, an execute run is
NEVER dispatched to the raw claude dispatcher. It requires an explicitly-wired
sandboxed executor; without one it is fail-closed (plan recorded, card moved to
review, no dispatch). Propose/dry-run are unaffected.
"""

from __future__ import annotations

from unittest import mock

import pytest

from skcapstone import agent_run as ar
from skcapstone.card_store import import_from_legacy
from skcapstone.coordination import Board, Task


@pytest.fixture
def home(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="t1", title="Do a thing", created_by="chef"))
    import_from_legacy(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_execute_dispatcher():
    ar.set_execute_dispatcher(None)
    yield
    ar.set_execute_dispatcher(None)


def test_execute_fail_closed_without_sandbox(home, monkeypatch):
    monkeypatch.setenv("SKAI_RUNNER_LIVE", "1")
    ar.request_run(home, "t1", "make the change", mode="execute")
    item = ar.list_queued(home)[0]
    raw = mock.Mock(return_value={"summary": "raw", "activity": [], "links": {}})
    out = ar.process_one(home, item, dispatcher=raw)
    assert out["gated"] is True
    assert "sandbox" in out["reason"].lower()
    raw.assert_not_called()  # the raw dispatcher is NEVER called for execute
    assert ar.current_run(home, "t1")["state"] == ar.NEEDS_REVIEW


def test_execute_uses_sandboxed_dispatcher_when_wired(home, monkeypatch):
    monkeypatch.setenv("SKAI_RUNNER_LIVE", "1")
    sandbox = mock.Mock(return_value={"summary": "draft PR opened", "activity": [], "links": {}})
    ar.set_execute_dispatcher(sandbox)
    raw = mock.Mock(return_value={"summary": "raw", "activity": [], "links": {}})
    ar.request_run(home, "t1", "make the change", mode="execute")
    item = ar.list_queued(home)[0]
    out = ar.process_one(home, item, dispatcher=raw)
    sandbox.assert_called_once()
    raw.assert_not_called()
    assert out["state"] == ar.NEEDS_REVIEW


def test_propose_uses_passed_dispatcher(home, monkeypatch):
    monkeypatch.setenv("SKAI_RUNNER_LIVE", "1")
    raw = mock.Mock(return_value={"summary": "planned", "activity": [], "links": {}})
    ar.request_run(home, "t1", "plan it", mode="propose")
    item = ar.list_queued(home)[0]
    out = ar.process_one(home, item, dispatcher=raw)
    raw.assert_called_once()
    assert out["state"] == ar.NEEDS_REVIEW


def test_dry_run_uses_passed_dispatcher(home, monkeypatch):
    monkeypatch.setenv("SKAI_RUNNER_LIVE", "1")
    raw = mock.Mock(return_value={"summary": "scratch diff", "activity": [], "links": {}})
    ar.request_run(home, "t1", "draft it", mode="dry-run")
    item = ar.list_queued(home)[0]
    out = ar.process_one(home, item, dispatcher=raw)
    raw.assert_called_once()
    assert out["state"] == ar.NEEDS_REVIEW


def test_claude_dispatcher_refuses_execute():
    out = ar.claude_dispatcher(
        {
            "card_id": "t1",
            "kind": "task",
            "title": "x",
            "instruction": "y",
            "agent": "lumina",
            "mode": "execute",
        }
    )
    assert "refused" in out["summary"].lower()
    assert out["activity"] and out["activity"][0]["atype"] == "error"


def test_execute_not_live_records_plan(home):
    # SKAI_RUNNER_LIVE unset -> nothing dispatches, plan recorded.
    ar.request_run(home, "t1", "make the change", mode="execute")
    item = ar.list_queued(home)[0]
    raw = mock.Mock()
    out = ar.process_one(home, item, dispatcher=raw)
    assert out.get("planned") is True
    raw.assert_not_called()


def test_execute_dispatch_available_toggle():
    assert ar.execute_dispatch_available() is False
    ar.set_execute_dispatcher(lambda ctx: {"summary": "ok"})
    assert ar.execute_dispatch_available() is True
    ar.set_execute_dispatcher(None)
    assert ar.execute_dispatch_available() is False
