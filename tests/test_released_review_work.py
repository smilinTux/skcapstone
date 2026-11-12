"""A release alone cannot replace the sealed direct-producer evidence."""

from skcapstone.seraph_review_cardstore import LiveCardStoreGateway
from tests.test_guarded_review_work import prepared
from tests.test_provisional_verdict_producer import _run


def test_unsealed_released_producer_cannot_open_review(tmp_path):
    store, card, owner, _, args = prepared(tmp_path)
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
    before = store._read_events(card)
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
    assert "guarded review source or claim changed" in result.output
    assert store._read_events(card) == before
    assert store.list_card_ids() == [card]
