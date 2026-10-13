"""Review opening preserves a released producer without impersonating its claim."""

import json

import pytest

from skcapstone.seraph_review_cardstore import LiveCardStoreGateway
from tests.test_guarded_review_work import prepared
from tests.test_provisional_verdict_producer import _run


def released(home):
    """Release the exact typed generation and compute the current read guard."""
    store, card, owner, artifact, args = prepared(home)
    before = LiveCardStoreGateway(home).read_card(card).revision
    claim = store.fold(card).meta["_claim_revision"]
    store.append_event(
        card,
        "release_claim",
        "sweeper",
        released_owner=owner,
        expected_claim_revision=claim,
        abandon_reason="error",
    )
    args[args.index("--expected-source-revision") + 1] = (
        LiveCardStoreGateway(home).read_card(card).revision
    )
    return store, card, owner, artifact, args, before, claim


def test_open_released_source_once_without_reclaim(tmp_path):
    store, card, owner, _, args, before, claim = released(tmp_path)
    history = store._read_events(card)
    result = _run(tmp_path, *args)
    assert result.exit_code == 0, result.output
    review_id = json.loads(result.output)["review_card_id"]
    review = store.fold(review_id)
    assert review.meta["producer_identity"] == owner
    assert review.meta["released_producer_claim"] == claim
    assert review.meta["source_revision"] == before
    again = _run(tmp_path, *args)
    assert again.exit_code == 0, again.output
    assert json.loads(again.output)["review_card_id"] == review_id
    assert json.loads(again.output)["created"] is False
    assert store._read_events(card) == history
    assert store.fold(card).owner is None
    assert len(store.list_card_ids()) == 2


@pytest.mark.parametrize(
    "change",
    [
        "claim",
        "assign",
        "unassign",
        "complete",
        "void",
        "tree",
        "repository",
        "outcome",
        "evidence",
    ],
)
def test_changed_released_generation_refuses_even_with_fresh_revision(tmp_path, change):
    store, card, _, artifact, args, _, _ = released(tmp_path)
    if change == "evidence":
        artifact.write_text("changed bytes")
    elif change in {"tree", "repository", "outcome"}:
        store.append_event(
            card,
            "link",
            "other",
            link_key="verdict" if change == "outcome" else change,
            link_value="BLOCKED" if change == "outcome" else "f" * 40,
        )
    else:
        store.append_event(card, change, "other", owner="other", claim_revision="d" * 32)
    try:
        revision = LiveCardStoreGateway(tmp_path).read_card(card).revision
    except ValueError:
        revision = "f" * 64  # Malformed bindings must refuse before any write.
    args[args.index("--expected-source-revision") + 1] = revision
    history = store._read_events(card)
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
    assert len(store.list_card_ids()) == 1
    assert store._read_events(card) == history


def test_review_opened_before_release_keeps_same_generation(tmp_path):
    store, card, owner, _, args = prepared(tmp_path)
    first = _run(tmp_path, *args)
    assert first.exit_code == 0, first.output
    review_id = json.loads(first.output)["review_card_id"]
    claim = store.fold(card).meta["_claim_revision"]
    store.append_event(
        card,
        "release_claim",
        "sweeper",
        released_owner=owner,
        expected_claim_revision=claim,
        abandon_reason="error",
    )
    args[args.index("--expected-source-revision") + 1] = (
        LiveCardStoreGateway(tmp_path).read_card(card).revision
    )
    replay = _run(tmp_path, *args)
    assert replay.exit_code == 0, replay.output
    assert json.loads(replay.output)["review_card_id"] == review_id
    assert json.loads(replay.output)["created"] is False
    assert len(store.list_card_ids()) == 2
