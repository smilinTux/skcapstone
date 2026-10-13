"""Opt-in native acceptance custody proof for an exactly released producer."""

import json

from skcoord.card_store import card_mutation_lock

from .fleet.source_custody import CustodyState, inspect_custody
from .seraph_review_cardstore import LiveCardStoreGateway, card_revision


def released_view(home, card_id, agent, claim, binding):
    """Revalidate captured content and exact release; caller holds the card lock."""
    try:
        source = json.loads(binding)
        if (source["card"], source["owner"], source["claim"]) != (card_id, agent, claim):
            raise ValueError("released source identity changed")
        proof = inspect_custody(home, card_id, agent, claim, source=source)
        if proof.state != CustodyState.RELEASED_CLEAN:
            raise ValueError("released source custody or content changed")
    except (KeyError, TypeError) as exc:
        raise ValueError("released source binding invalid") from exc
    return proof.before_release()


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
