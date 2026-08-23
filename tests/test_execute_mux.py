"""Tests for the execute-mode mux dispatcher (P4, card c6a87139).

Proves: a repo-labeled card always routes to the code dispatcher; an
alert/gtd-origin card with no repo label routes to the comms dispatcher; a
card with neither signal (or an unfoldable id) falls through to the code
dispatcher unchanged from today's behavior; and a ``None`` leg never gets
called, producing a well-formed "not wired" result instead of a crash.
"""

from __future__ import annotations

from unittest import mock

from skcapstone import agent_run as ar
from skcapstone import alert_store
from skcapstone.execute_mux import build_execute_mux


def _seed_alert_card(home, alert_id="a1", extra_labels=None):
    alert_store.raise_alert(home, alert_id, "t", options=["opt"], labels=extra_labels or [])
    ar.ensure_card(home, f"alert-{alert_id}")
    return f"alert-{alert_id}"


def test_repo_labeled_card_routes_to_code_dispatcher(tmp_path):
    from skcapstone.card_store import CardCore, CardStore

    CardStore(tmp_path).create(
        CardCore(id="t1", kind="task", title="fix it", initial_labels=["repo:skcapstone"])
    )
    code = mock.Mock(return_value={"summary": "code", "activity": [], "links": {}})
    comms = mock.Mock(return_value={"summary": "comms", "activity": [], "links": {}})
    mux = build_execute_mux(tmp_path, code, comms)

    out = mux({"card_id": "t1", "instruction": "x"})

    code.assert_called_once()
    comms.assert_not_called()
    assert out["summary"] == "code"


def test_alert_card_with_no_repo_label_routes_to_comms_dispatcher(tmp_path):
    card_id = _seed_alert_card(tmp_path)
    code = mock.Mock(return_value={"summary": "code", "activity": [], "links": {}})
    comms = mock.Mock(return_value={"summary": "comms", "activity": [], "links": {}})
    mux = build_execute_mux(tmp_path, code, comms)

    out = mux({"card_id": card_id, "instruction": "x"})

    comms.assert_called_once()
    code.assert_not_called()
    assert out["summary"] == "comms"


def test_repo_label_wins_over_alert_origin(tmp_path):
    """An explicit repo label is a stronger, more specific signal than the
    origin surface: a card can carry both (e.g. an alert about a broken
    build, tagged to the repo that needs the fix)."""
    card_id = _seed_alert_card(tmp_path, extra_labels=["repo:skcapstone"])
    code = mock.Mock(return_value={"summary": "code", "activity": [], "links": {}})
    comms = mock.Mock(return_value={"summary": "comms", "activity": [], "links": {}})
    mux = build_execute_mux(tmp_path, code, comms)

    mux({"card_id": card_id, "instruction": "x"})

    code.assert_called_once()
    comms.assert_not_called()


def test_unknown_card_falls_through_to_code_dispatcher(tmp_path):
    code = mock.Mock(return_value={"summary": "code", "activity": [], "links": {}})
    comms = mock.Mock(return_value={"summary": "comms", "activity": [], "links": {}})
    mux = build_execute_mux(tmp_path, code, comms)

    mux({"card_id": "does-not-exist", "instruction": "x"})

    code.assert_called_once()
    comms.assert_not_called()


def test_no_card_id_falls_through_to_code_dispatcher(tmp_path):
    code = mock.Mock(return_value={"summary": "code", "activity": [], "links": {}})
    comms = mock.Mock(return_value={"summary": "comms", "activity": [], "links": {}})
    mux = build_execute_mux(tmp_path, code, comms)

    mux({"instruction": "x"})

    code.assert_called_once()
    comms.assert_not_called()


def test_alert_card_but_comms_dispatcher_unwired_is_not_a_crash(tmp_path):
    card_id = _seed_alert_card(tmp_path)
    code = mock.Mock(return_value={"summary": "code", "activity": [], "links": {}})
    mux = build_execute_mux(tmp_path, code, None)

    out = mux({"card_id": card_id, "instruction": "x"})

    code.assert_not_called()
    assert out["links"] == {}
    assert "not dispatched" in out["summary"].lower()


def test_repo_labeled_card_but_code_dispatcher_unwired_is_not_a_crash(tmp_path):
    from skcapstone.card_store import CardCore, CardStore

    CardStore(tmp_path).create(
        CardCore(id="t1", kind="task", title="fix it", initial_labels=["repo:skcapstone"])
    )
    comms = mock.Mock(return_value={"summary": "comms", "activity": [], "links": {}})
    mux = build_execute_mux(tmp_path, None, comms)

    out = mux({"card_id": "t1", "instruction": "x"})

    comms.assert_not_called()
    assert out["links"] == {}
    assert "not dispatched" in out["summary"].lower()


def test_mux_is_marked_for_idempotent_rewiring():
    mux = build_execute_mux("home", None, None)
    assert getattr(mux, "_is_execute_mux", False) is True
