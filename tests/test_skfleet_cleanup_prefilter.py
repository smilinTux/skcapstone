"""Historical cleanup exclusions retain fresh checks for mutation candidates."""

import os
import re
from types import SimpleNamespace

from tests.test_skfleet_review_claim_release import _load


def test_historical_parent_outcomes_need_no_native_or_lifecycle_folds():
    """A large historical review index costs only cached outcome lookups."""
    parents = {f"{index:08x}": ["abcdef01"] for index in range(2000)}
    namespace = {
        "HOST": "chiap08",
        "AUTHORITY_HOST": "chiap08",
        "PRODUCTION_POLICY": {"authority_host": "chiap08"},
        "_load_outcomes": lambda: {
            card: ("stamp", "PASS" if index % 2 else "BLOCKED")
            for index, card in enumerate(parents)
        },
        "_reviews_by_parent": lambda: parents,
        "_PROVISIONAL_PASS_RE": re.compile(r"^PASS_FOR_REVIEW"),
    }
    # No filesystem, lifecycle, CardStore, or subprocess implementation is
    # supplied: any historical fold or attempted mutation must fail the test.
    _load("close_reviewed_parents", namespace)
    assert namespace["close_reviewed_parents"]() == 0


def test_closed_provisional_parents_skip_new_native_stores():
    """Existing lifecycle snapshots reject old provisional generations."""
    parents = {f"{index:08x}": ["abcdef01"] for index in range(2000)}
    reads = []
    namespace = {
        "HOST": "chiap08",
        "AUTHORITY_HOST": "chiap08",
        "PRODUCTION_POLICY": {"authority_host": "chiap08"},
        "CARDS": "/cards",
        "os": SimpleNamespace(path=SimpleNamespace(join=os.path.join, isdir=lambda _: True)),
        "_load_outcomes": lambda: {card: ("stamp", "PASS_FOR_REVIEW") for card in parents},
        "_reviews_by_parent": lambda: parents,
        "_PROVISIONAL_PASS_RE": re.compile(r"^PASS_FOR_REVIEW"),
        "lifecycle_state": lambda card: reads.append(card) or "complete",
    }
    _load("close_reviewed_parents", namespace)
    assert namespace["close_reviewed_parents"]() == 0
    assert reads == list(parents)


def release_namespace(cards):
    """Provide traversal without any mutation or fresh-read capability."""
    return {
        "HOST": "chiap08",
        "AUTHORITY_HOST": "chiap08",
        "DRY": False,
        "PRODUCTION_POLICY": {"authority_host": "chiap08"},
        "CARDS": "/cards",
        "glob": SimpleNamespace(glob=lambda _: ["/cards/" + card for card in cards]),
        "os": os,
        "re": re,
    }


def test_nonlocal_unowned_and_historical_cards_need_no_fresh_claim_reads():
    """Cached rejection defers newly claimed cards without touching custody."""
    cards = [f"{index:08x}" for index in range(2000)]
    reads = []

    def cached(card):
        reads.append(card)
        index = int(card, 16)
        return (None, f"pi-seraph-chiap02-{card}", "jarvis")[index % 3], 1

    namespace = release_namespace(cards)
    namespace["_current_claim"] = cached
    _load("release_finished_review_claims", namespace)
    assert namespace["release_finished_review_claims"]() == 0
    assert reads == cards


def test_stale_cached_local_owner_cannot_authorize_a_fresh_foreign_claim():
    """An actual candidate still reads current custody before any processing."""
    card = "abcdef01"
    reads = []
    namespace = release_namespace([card])
    namespace["_current_claim"] = lambda _: (f"pi-seraph-chiap08-{card}", 1)
    namespace["_current_claim_identity_fresh"] = lambda cid: reads.append(cid) or (
        "jarvis",
        2,
        "new-claim",
    )
    _load("release_finished_review_claims", namespace)
    assert namespace["release_finished_review_claims"]() == 0
    assert reads == [card]
