"""Opt-in native acceptance custody proof for an exactly released producer."""

import json

from skcoord.card_store import CardStore, card_mutation_lock

from .fleet.production_review_custody import released_source_matches
from .seraph_review_cardstore import LiveCardStoreGateway, card_revision


class _WithoutRelease(CardStore):
    """Read-only counterfactual: retain every event except one proven release."""

    def __init__(self, home, release):
        super().__init__(home)
        self.release = release

    def _read_events(self, card_id):
        return [e for e in super()._read_events(card_id) if e != self.release]

    def _legacy_events(self, card_id):
        return [e for e in super()._legacy_events(card_id) if e != self.release]


def released_view(home, card_id, agent, claim, binding):
    """Revalidate captured content and exact release; caller holds the card lock."""
    try:
        source = json.loads(binding)
        if (source["card"], source["owner"], source["claim"]) != (card_id, agent, claim):
            raise ValueError("released source identity changed")
        revision = LiveCardStoreGateway(home).read_card(card_id).revision
        if not released_source_matches(home, source, revision):
            raise ValueError("released source custody or content changed")
    except (KeyError, TypeError) as exc:
        raise ValueError("released source binding invalid") from exc
    store = CardStore(home)
    # The shared proof above establishes that the latest custody event is this release.
    release = max(
        (
            e
            for e in store._read_events(card_id) + store._legacy_events(card_id)
            if e.get("action") == "release_claim"
        ),
        key=lambda e: (e.get("ts", ""), e.get("writer", ""), e.get("seq", 0)),
    )
    view = _WithoutRelease(home, release)
    row = view.fold(card_id)
    if row.owner != agent or row.meta.get("_claim_revision") != claim:
        raise ValueError("release did not relinquish the captured claim")
    return view


def released_revision_guard(home, agent, card_id, revision, claim, binding):
    """Fence a new write to its exact revision, allowing only the proven release."""
    from .coord_completion import owned_revision_guard

    owned_revision_guard(home, agent, card_id, revision, claim)  # Validate guard shapes.

    def verify():
        with card_mutation_lock(home, card_id):
            view = released_view(home, card_id, agent, claim, binding)
            gateway = LiveCardStoreGateway(home)
            if revision not in {
                gateway.read_card(card_id).revision,
                gateway.read_card(card_id, _store=view).revision,
            }:
                raise ValueError("released source revision changed")

    return verify


def released_replay_matches(home, card_id, agent, claim, binding, after_revision):
    """Permit a lost reply across release, without hiding any other changed facts."""
    view = released_view(home, card_id, agent, claim, binding)
    return card_revision(view.fold(card_id)) == after_revision
