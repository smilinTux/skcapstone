"""The trusted relay cannot close a different claim or source generation."""

import pytest

from skcapstone.card_store import CardStore
from skcapstone.coordination import Board
from skcapstone.seraph_review_cardstore import LiveCardStoreGateway
from tests.test_provisional_verdict_producer import _run, _seed


def prepared(home):
    """Seed one isolated claimed card, without consulting live admission."""
    card, owner, claim = "1234abcd", "pi-guard-complete", "c" * 32
    _seed(home, card)
    store = CardStore(home)
    store.append_event(card, "claim", owner, owner=owner, claim_revision=claim)
    revision = LiveCardStoreGateway(home).read_card(card).revision
    args = [
        "complete",
        card,
        "--agent",
        owner,
        "--expected-source-revision",
        revision,
        "--expected-claim-revision",
        claim,
    ]
    return store, card, owner, args


def test_guarded_complete_uses_existing_native_completion(tmp_path):
    store, card, owner, args = prepared(tmp_path)
    result = _run(tmp_path, *args)
    assert result.exit_code == 0, result.output
    assert store.fold(card).status.value == "done"
    assert store.fold(card).owner is None
    before = store._read_events(card)
    repeated = _run(tmp_path, *args)
    assert repeated.exit_code != 0
    assert store._read_events(card) == before


@pytest.mark.parametrize("change", ["owner", "claim", "contract", "link", "outcome"])
def test_changed_owned_generation_is_not_completed(tmp_path, change):
    store, card, owner, args = prepared(tmp_path)
    if change == "owner":
        store.append_event(card, "unassign", "operator")
    elif change == "claim":
        store.append_event(card, "claim", owner, owner=owner, claim_revision="d" * 32)
    elif change == "contract":
        store.append_event(card, "describe", owner, description="changed contract")
    elif change == "link":
        store.append_event(card, "link", owner, link_key="evidence", link_value="changed")
    else:
        store.append_event(card, "verdict", owner, verdict="FAIL")
    before = store._read_events(card)
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
    assert store._read_events(card) == before
    assert store.fold(card).status.value != "done"


@pytest.mark.parametrize("flag", ["--expected-source-revision", "--expected-claim-revision"])
def test_partial_completion_guards_do_not_fall_back_to_legacy(tmp_path, flag):
    store, card, owner, args = prepared(tmp_path)
    index = args.index(flag)
    del args[index : index + 2]
    before = store._read_events(card)
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
    assert store._read_events(card) == before


def test_claim_race_is_rechecked_inside_native_completion_lock(tmp_path, monkeypatch):
    store, card, owner, args = prepared(tmp_path)
    original = Board.complete_task

    def raced(self, agent, task, **kwargs):
        store.append_event(card, "claim", owner, owner=owner, claim_revision="e" * 32)
        return original(self, agent, task, **kwargs)

    monkeypatch.setattr(Board, "complete_task", raced)
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
    assert not [e for e in store._read_events(card) if e["action"] == "complete"]
    assert store.fold(card).owner == owner


def test_legacy_complete_still_uses_unchanged_contract(tmp_path):
    store, card, owner, args = prepared(tmp_path)
    result = _run(tmp_path, "complete", card, "--agent", owner)
    assert result.exit_code == 0, result.output
    assert store.fold(card).status.value == "done"


def test_related_mutation_guard_cannot_silently_use_legacy_completion(tmp_path):
    from skcapstone.coord_completion import complete_coord_task

    store, card, owner, args = prepared(tmp_path)
    with pytest.raises(ValueError, match="requires ownership guard"):
        complete_coord_task(tmp_path, owner, card, mutation_precondition=lambda: None)
    assert store.fold(card).owner == owner
