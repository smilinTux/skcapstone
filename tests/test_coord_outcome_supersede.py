"""Governed supersession for a producer whose review custody was lost."""

from __future__ import annotations

from click.testing import CliRunner

from skcapstone.card_store import CardCore, CardStore
from skcapstone.cli.coord import register_coord_commands
from skcapstone.coordination import Board, Task


def _main():
    import click

    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _seed(home, card_id="deadbeef"):
    board = Board(home)
    board.ensure_dirs()
    board.create_task(Task(id=card_id, title="Producer", description="body"))
    store = CardStore(home)
    store.create(CardCore(id=card_id, title="Producer", description="body"))
    store.append_event(card_id, "move", "jarvis", column="ready")
    outcome = store.append_event(card_id, "verdict", "glm-worker", verdict="PASS_FOR_REVIEW")
    return store, outcome["event_id"]


def _run(home, card_id, event_id, reason="review custody lost"):
    return CliRunner().invoke(
        _main(),
        [
            "coord",
            "outcome-supersede",
            card_id,
            reason,
            "--expected-outcome-event",
            event_id,
            "--agent",
            "jarvis",
            "--home",
            str(home),
        ],
    )


def test_outcome_supersede_is_governed_and_bound_to_current_pass(tmp_path, monkeypatch):
    monkeypatch.setattr("skcapstone.jarvis_emergency.authorize_coord_mutation", lambda *args: None)
    store, event_id = _seed(tmp_path)

    result = _run(tmp_path, "deadbeef", event_id)

    assert result.exit_code == 0, result.output
    store = CardStore(tmp_path)
    events = store._read_events("deadbeef") + store._legacy_events("deadbeef")
    supersession = [
        event
        for event in events
        if event.get("action") == "link" and event.get("link_key") == "verdict_superseded"
    ]
    assert len(supersession) == 1
    assert supersession[0]["link_value"] == (
        f"SUPERSEDED prior_event={event_id} reason=review custody lost"
    )


def test_outcome_supersede_refuses_stale_event_without_appending(tmp_path, monkeypatch):
    monkeypatch.setattr("skcapstone.jarvis_emergency.authorize_coord_mutation", lambda *args: None)
    store, _event_id = _seed(tmp_path)
    before = store._legacy_events("deadbeef")

    result = _run(tmp_path, "deadbeef", "f" * 32)

    assert result.exit_code != 0
    assert "expected PASS_FOR_REVIEW" in result.output
    assert store._legacy_events("deadbeef") == before


def test_outcome_supersede_refuses_claimed_producer(tmp_path, monkeypatch):
    monkeypatch.setattr("skcapstone.jarvis_emergency.authorize_coord_mutation", lambda *args: None)
    store, event_id = _seed(tmp_path)
    store.append_event(
        "deadbeef", "claim", "glm-worker", owner="glm-worker", claim_revision="a" * 32
    )

    result = _run(tmp_path, "deadbeef", event_id)

    assert result.exit_code != 0
    assert "unowned, active READY" in result.output
    assert not any(
        event.get("link_key") == "verdict_superseded" for event in store._legacy_events("deadbeef")
    )
