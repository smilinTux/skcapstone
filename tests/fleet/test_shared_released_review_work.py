"""The upstream sealed direct-review path retains shared custody fences."""

import pytest

from tests.fleet.test_released_direct_review import open_review, released_direct  # noqa: F401
from tests.fleet.test_source_bundle import source  # noqa: F401


@pytest.mark.parametrize("source", ["https"], indirect=True)
@pytest.mark.parametrize(
    "change", ["assign", "unassign", "complete", "void", "tree", "description"]
)
def test_released_review_refuses_later_drift(released_direct, change):  # noqa: F811
    v = released_direct
    store, card = v["store"], v["card"]
    if change == "tree":
        store.append_event(card, "link", "other", link_key="tree", link_value="f" * 40)
    elif change == "description":
        store.append_event(card, "describe", "other", description="changed scope")
    else:
        store.append_event(card, change, "other", owner="other", claim_revision="d" * 32)
    before = store._read_events(card)
    with pytest.raises(ValueError):
        open_review(v)
    assert store._read_events(card) == before
    assert store.list_card_ids() == [card]


@pytest.mark.parametrize("source", ["https"], indirect=True)
def test_released_review_uses_shared_proof(released_direct, monkeypatch):  # noqa: F811
    from skcapstone.fleet import released_source
    from skcapstone.fleet.source_custody import CustodyState

    v = released_direct
    original, states = released_source.inspect_custody, []

    def inspect(*args, **kwargs):
        result = original(*args, **kwargs)
        states.append(result.state)
        return result

    monkeypatch.setattr(released_source, "inspect_custody", inspect)
    before = v["store"]._read_events(v["card"])
    first = open_review(v)
    assert first.launchable
    assert open_review(v).review_card_id == first.review_card_id
    assert states and set(states) == {CustodyState.RELEASED_CLEAN}
    assert v["store"]._read_events(v["card"]) == before


@pytest.mark.parametrize("source", ["https"], indirect=True)
def test_cross_host_claim_drift_after_reconciliation_refuses(
    released_direct, monkeypatch  # noqa: F811
):
    from skcapstone import guarded_review_work as opener

    v = released_direct
    original = opener.reconcile_review_work

    def race(*args, **kwargs):
        result = original(*args, **kwargs)
        v["store"].append_event(
            v["card"], "claim", "other", owner="other", claim_revision="d" * 32
        )
        return result

    monkeypatch.setattr(opener, "reconcile_review_work", race)
    with pytest.raises(ValueError, match="source changed during reconciliation"):
        open_review(v)
    assert v["store"].fold(v["card"]).owner == "other"
