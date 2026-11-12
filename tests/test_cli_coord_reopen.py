"""Focused coverage for the governed ``coord reopen`` lifecycle command."""

from __future__ import annotations

import copy

import click
import pytest
from click.testing import CliRunner
from skcoord.card import Column
from skcoord.card_store import CardCore, CardStore

from skcapstone.cli.coord import register_coord_commands


def _main() -> click.Group:
    @click.group()
    def main() -> None:
        pass

    register_coord_commands(main)
    return main


def _card(home, card_id: str = "a1b2c3d4") -> CardStore:
    store = CardStore(home)
    store.create(CardCore(id=card_id, kind="task", title="probe", created_by="test"))
    return store


def _complete(store: CardStore, card_id: str = "a1b2c3d4") -> None:
    store.append_event(card_id, "claim", "worker", owner="worker")
    store.append_event(card_id, "complete", "worker")


def _invoke(home, *extra: str):
    return CliRunner().invoke(
        _main(),
        [
            "coord",
            "reopen",
            "a1b2c3d4",
            "--reason",
            "open a fresh candidate-bound review",
            "--agent",
            "jarvis",
            "--home",
            str(home),
            *extra,
        ],
    )


def test_reopen_appends_one_attributed_event_and_preserves_completion(tmp_path, monkeypatch):
    store = _card(tmp_path)
    _complete(store)
    before = list(store._read_events("a1b2c3d4"))
    monkeypatch.setattr(
        "skcapstone.jarvis_emergency.authorize_coord_mutation",
        lambda *args, **kwargs: None,
    )

    result = _invoke(tmp_path)

    assert result.exit_code == 0, result.output
    events = store._read_events("a1b2c3d4")
    assert events[:-1] == before
    assert events[-1]["action"] == "reopen"
    assert events[-1]["writer"] == "jarvis"
    assert events[-1]["reason"] == "open a fresh candidate-bound review"
    assert events[-1]["column"] == "backlog"
    assert len(events[-1]["previous_state_sha256"]) == 64
    assert store.fold("a1b2c3d4").status == Column.BACKLOG
    assert any(event["action"] == "complete" for event in events)


@pytest.mark.parametrize("state", ["open", "owned", "archived", "voided"])
def test_reopen_refuses_ineligible_state_without_append(tmp_path, monkeypatch, state):
    store = _card(tmp_path)
    if state != "open":
        _complete(store)
    if state == "owned":
        store.append_event("a1b2c3d4", "assign", "other", owner="other")
    elif state == "archived":
        store.append_event("a1b2c3d4", "archive", "other")
    elif state == "voided":
        store.append_event("a1b2c3d4", "void", "other", reason="withdrawn")
    before = list(store._read_events("a1b2c3d4"))
    monkeypatch.setattr(
        "skcapstone.jarvis_emergency.authorize_coord_mutation",
        lambda *args, **kwargs: None,
    )

    result = _invoke(tmp_path)

    assert result.exit_code != 0
    assert store._read_events("a1b2c3d4") == before
    assert not any(event["action"] == "reopen" for event in before)


def test_reopen_refuses_blank_reason_without_append(tmp_path, monkeypatch):
    store = _card(tmp_path)
    _complete(store)
    before = list(store._read_events("a1b2c3d4"))
    monkeypatch.setattr(
        "skcapstone.jarvis_emergency.authorize_coord_mutation",
        lambda *args, **kwargs: None,
    )

    result = CliRunner().invoke(
        _main(),
        [
            "coord",
            "reopen",
            "a1b2c3d4",
            "--reason",
            "   ",
            "--agent",
            "jarvis",
            "--home",
            str(tmp_path),
        ],
    )

    assert result.exit_code != 0
    assert "reason" in result.output.lower()
    assert store._read_events("a1b2c3d4") == before


def test_reopen_rechecks_state_under_lock_and_refuses_stale_snapshot(tmp_path, monkeypatch):
    store = _card(tmp_path)
    _complete(store)
    before = list(store._read_events("a1b2c3d4"))
    original_fold = CardStore.fold
    calls = 0

    def changed_fold(self, card_id):
        nonlocal calls
        card = original_fold(self, card_id)
        calls += 1
        if calls >= 2 and card is not None:
            card = copy.deepcopy(card)
            card.status = Column.DOING
        return card

    monkeypatch.setattr(CardStore, "fold", changed_fold)
    monkeypatch.setattr(
        "skcapstone.jarvis_emergency.authorize_coord_mutation",
        lambda *args, **kwargs: None,
    )

    result = _invoke(tmp_path)

    assert result.exit_code != 0
    assert "changed while acquiring" in result.output
    assert store._read_events("a1b2c3d4") == before
