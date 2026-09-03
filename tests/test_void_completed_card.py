"""coord void must not report a state change the fold will discard.

Regression test for the 2026-08-27 finding that `coord void` accepted an
already-completed card, printed success, appended a void event, and changed
nothing: the card still folded to done.

The mechanism, established by these tests rather than assumed: CardStore.fold
has NO handler for action == "void". It handles "archive" and "reopen" and
ignores "void" entirely. So voiding a card is expressed by archiving it off the
active board, never by its folded status, and `is_voided` is a separate lookup
for the audit event. On an already-completed card there is nothing left to
archive off the board, so the void becomes a pure no-op that still reports
success.
"""

import json

import pytest
from skcoord.card import Column
from skcoord.card_store import CardCore, CardStore

from skcapstone.coord_amendments import is_voided, void_card
from skcapstone.coordination import Board

_SEQ = iter("0123456789abcdef" * 8)


def _new_card(home, title="probe"):
    """Create a real card in a temp store and return its id."""
    cid = "aaaaaaa" + next(_SEQ)
    CardStore(home).create(CardCore(id=cid, kind="task", title=title, created_by="test"))
    return cid


def test_voids_an_open_card(tmp_path):
    cid = _new_card(tmp_path)
    void_card(tmp_path, cid, reason="raised by mistake", agent="test")
    assert is_voided(tmp_path, cid)


def test_void_archives_every_projection_and_preserves_reason(tmp_path):
    cid = _new_card(tmp_path)
    reason = "Superseded by successor01"

    void_card(tmp_path, cid, reason=reason, agent="chef")

    store = CardStore(tmp_path)
    folded = store.fold(cid)
    events = store._read_events(cid)
    void = next(event for event in events if event["action"] == "void")
    assert folded is not None and folded.archived is True
    assert cid in Board(tmp_path).archived_ids()
    assert void["writer"] == "chef"
    assert void["reason"] == reason


def test_void_fails_loudly_when_archive_does_not_reach_store(tmp_path, monkeypatch):
    cid = _new_card(tmp_path)

    def legacy_only_archive(self, task_id, by=""):
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        index = self.archive_dir / "synthetic.jsonl"
        entry = {"id": task_id, "archived_at": "now", "archived_by": by}
        encoded = json.dumps(entry)
        json.loads(encoded)
        index.write_text(encoded + "\n", encoding="utf-8")

    monkeypatch.setattr(Board, "archive_task", legacy_only_archive)

    with pytest.raises(RuntimeError, match="partially applied.*CardStore"):
        void_card(tmp_path, cid, reason="must not report success", agent="test")

    assert is_voided(tmp_path, cid)
    events = CardStore(tmp_path)._read_events(cid)
    void_index = next(i for i, event in enumerate(events) if event["action"] == "void")
    assert not any(event["action"] == "archive" for event in events[void_index + 1 :])


def test_refuses_a_completed_card(tmp_path):
    cid = _new_card(tmp_path)
    store = CardStore(tmp_path)
    store.append_event(cid, "complete", "test")
    assert store.fold(cid).status == Column.DONE

    with pytest.raises(ValueError) as err:
        void_card(tmp_path, cid, reason="completion was false", agent="test")

    # the message must name the alternative, not just refuse
    assert "already complete" in str(err.value)
    assert "supersede" in str(err.value).lower()
    # and crucially: nothing was written
    assert not is_voided(tmp_path, cid)


def test_force_terminal_records_but_does_not_change_state(tmp_path):
    cid = _new_card(tmp_path)
    store = CardStore(tmp_path)
    store.append_event(cid, "complete", "test")

    void_card(tmp_path, cid, reason="audit trace", agent="test", force_terminal=True)

    # the audit trace exists...
    assert is_voided(tmp_path, cid)
    # ...and the sticky-terminal rule is UNCHANGED: it still folds to done.
    assert store.fold(cid).status == Column.DONE


def test_fold_ignores_the_void_action_entirely(tmp_path):
    """The mechanism behind the defect, pinned so nobody re-derives it wrongly.

    A void event does not appear in the folded status at all. Voiding is
    expressed by archival; the event itself is an audit record read through
    `is_voided`. This is why voiding a completed card changed nothing.
    """
    cid = _new_card(tmp_path)
    store = CardStore(tmp_path)
    store.append_event(cid, "complete", "test")
    before = store.fold(cid).status
    store.append_event(cid, "void", "test", reason="direct event, no archival")
    assert store.fold(cid).status == before == Column.DONE
    # ...yet the audit lookup still sees it
    assert is_voided(tmp_path, cid)


def test_completed_card_is_reopenable_by_claim(tmp_path):
    """Recorded because it contradicts an assumption worth not repeating.

    The fleet rotation folds terminal states as STICKY: once complete, later
    lifecycle events are ignored. The canonical CardStore fold does not do that;
    a claim after a completion moves the card back to doing. The two folds
    disagree, and this test pins the canonical behaviour so the divergence is
    visible rather than discovered again in production.
    """
    cid = _new_card(tmp_path)
    store = CardStore(tmp_path)
    store.append_event(cid, "complete", "test")
    assert store.fold(cid).status == Column.DONE
    store.append_event(cid, "claim", "someone-else", owner="someone-else")
    assert store.fold(cid).status == Column.DOING


def test_empty_reason_still_rejected(tmp_path):
    cid = _new_card(tmp_path)
    with pytest.raises(ValueError):
        void_card(tmp_path, cid, reason="", agent="test")


def test_double_void_still_rejected(tmp_path):
    cid = _new_card(tmp_path)
    void_card(tmp_path, cid, reason="first", agent="test")
    with pytest.raises(ValueError) as err:
        void_card(tmp_path, cid, reason="second", agent="test")
    assert "already voided" in str(err.value)
