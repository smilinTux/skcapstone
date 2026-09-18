"""A parked card returns to the pool only when its blocker genuinely completed.

The defect these guard: ``blocked_backoff`` wakes a card when its blocker
CHANGES after the BLOCKED verdict. A blocker that was ALREADY terminal when the
verdict was written can never change again, so the card is parked forever.
Measured on chiap01 2026-09-18, of 206 open cards whose latest outcome is
BLOCKED, eleven name a referent that folded to DONE BEFORE their own verdict,
one of them by half a second.

The repair returns those cards, and ONLY those. Every other shape stays with an
operator, because the cost of a wrong reopen is a worker re-deriving a real wall
and a human losing the signal that something needs deciding.
"""

from __future__ import annotations

import pytest

from skcapstone.blocker_referent import (
    AUTO_REOPEN_CATEGORIES,
    blocker_generation_id,
    settled_blocker_reopen,
)

DONE = {"state": "complete", "human_gated": False, "outcome_blocked": False, "completed_at": 100.0}


def facts_for(table):
    """A referent-fact lookup that fails closed on an unknown card."""

    def lookup(ref_id):
        return table.get(
            ref_id,
            {
                "state": "missing",
                "human_gated": False,
                "outcome_blocked": True,
                "completed_at": 0.0,
            },
        )

    return lookup


def decide(category, referents, table=None, card_id="aaaaaaaa"):
    return settled_blocker_reopen(card_id, category, referents, facts_for(table or {}))


# --- the case this exists for -------------------------------------------------


def test_a_card_returns_when_its_only_blocker_is_done():
    decision = decide("dependency", ["card:bbbbbbbb"], {"bbbbbbbb": DONE})
    assert decision["reopen"] is True
    assert decision["hold"] is None
    assert decision["referents"] == ["card:bbbbbbbb"]
    assert decision["transition_id"]


def test_completion_earlier_than_the_verdict_still_returns_the_card():
    """The whole defect. A referent that completed first never changes again."""
    early = dict(DONE, completed_at=1.0)
    assert decide("card", ["card:bbbbbbbb"], {"bbbbbbbb": early})["reopen"] is True


def test_every_blocker_must_be_done_not_just_one():
    table = {"bbbbbbbb": DONE, "cccccccc": dict(DONE, state="open")}
    decision = decide("card", ["card:bbbbbbbb", "card:cccccccc"], table)
    assert decision["reopen"] is False
    assert "open" in decision["hold"]


# --- fail closed --------------------------------------------------------------


@pytest.mark.parametrize("category", ["human", "capability", None, "", "unknown"])
def test_only_dependency_and_card_blockers_are_machine_dischargeable(category):
    """A human hold and a capability hold are not answered by a card finishing."""
    decision = decide(category, ["card:bbbbbbbb"], {"bbbbbbbb": DONE})
    assert decision["reopen"] is False
    assert decision["hold"].startswith("category:")


def test_the_two_dischargeable_categories_are_exactly_dependency_and_card():
    assert AUTO_REOPEN_CATEGORIES == frozenset({"dependency", "card"})


def test_a_block_with_no_recorded_referent_is_left_for_an_operator():
    assert decide("dependency", [])["hold"] == "no-referent"


@pytest.mark.parametrize(
    "referent",
    ["card:57c301a1:", "ac:3", "free", "approval:chef", "card:zzzzzzzz", "card:bbbbbbb"],
)
def test_a_referent_that_is_not_an_exact_card_id_is_never_discharged(referent):
    """Observed on chiap01: 57c201a1 cites `card:57c301a1:`, which resolves to
    no card at all. A referent the parser cannot pin exactly fails closed."""
    decision = decide("dependency", [referent], {"bbbbbbbb": DONE})
    assert decision["reopen"] is False
    assert decision["hold"].startswith("referent-not-a-card")


def test_a_missing_referent_card_is_never_discharged():
    assert decide("card", ["card:bbbbbbbb"])["hold"] == "referent-missing"


@pytest.mark.parametrize("state", ["void", "open", "claimed", "ambiguous"])
def test_a_referent_that_is_not_complete_holds_the_card(state):
    """`void` is the fold's answer for archived-without-completion AND for a
    voided card. Neither is a completion, so neither returns anything."""
    table = {"bbbbbbbb": dict(DONE, state=state)}
    assert decide("card", ["card:bbbbbbbb"], table)["hold"] == "referent-" + state


def test_a_referent_done_in_column_but_blocked_in_verdict_is_not_a_completion():
    """Three of the sixteen candidates on chiap01 are exactly this shape:
    885037c0, 9df37866 and bd795bb2 name a referent that folded to DONE while
    its own latest outcome still reads BLOCKED. Column is not evidence."""
    table = {"bbbbbbbb": dict(DONE, outcome_blocked=True)}
    assert decide("card", ["card:bbbbbbbb"], table)["hold"] == "referent-outcome-blocked"


def test_a_human_gated_referent_stays_with_the_human():
    table = {"bbbbbbbb": dict(DONE, human_gated=True)}
    assert decide("card", ["card:bbbbbbbb"], table)["hold"] == "referent-human-gated"


def test_a_completion_with_no_timestamp_cannot_be_attributed_and_holds():
    table = {"bbbbbbbb": dict(DONE, completed_at=0.0)}
    assert decide("card", ["card:bbbbbbbb"], table)["hold"] == "referent-completion-unstamped"


# --- idempotency --------------------------------------------------------------


def test_the_generation_id_is_stable_for_the_same_blocker_generation():
    table = {"bbbbbbbb": DONE}
    first = decide("card", ["card:bbbbbbbb"], table)["transition_id"]
    second = decide("dependency", ["card:bbbbbbbb"], table)["transition_id"]
    assert first == second, "the generation is the blocker, not the verdict wording"


def test_referent_order_does_not_change_the_generation_id():
    table = {"bbbbbbbb": DONE, "cccccccc": dict(DONE, completed_at=200.0)}
    forward = decide("card", ["card:bbbbbbbb", "card:cccccccc"], table)["transition_id"]
    reverse = decide("card", ["card:cccccccc", "card:bbbbbbbb"], table)["transition_id"]
    assert forward == reverse


def test_a_different_card_gets_a_different_generation_id():
    table = {"bbbbbbbb": DONE}
    mine = decide("card", ["card:bbbbbbbb"], table, card_id="aaaaaaaa")["transition_id"]
    theirs = decide("card", ["card:bbbbbbbb"], table, card_id="dddddddd")["transition_id"]
    assert mine != theirs


def test_a_referent_completing_again_later_is_a_new_generation():
    """A card re-blocked on a referent that has since been completed AGAIN is a
    genuinely new fact and may return again. Re-blocking on the SAME settled
    generation is not, which is what stops a block/reopen loop."""
    first = decide("card", ["card:bbbbbbbb"], {"bbbbbbbb": DONE})["transition_id"]
    later = decide("card", ["card:bbbbbbbb"], {"bbbbbbbb": dict(DONE, completed_at=900.0)})
    assert later["transition_id"] != first


def test_the_generation_id_is_namespaced_so_an_operator_can_read_it():
    assert decide("card", ["card:bbbbbbbb"], {"bbbbbbbb": DONE})["transition_id"].startswith(
        "blocker-settled:"
    )
    assert blocker_generation_id("aaaaaaaa", [("card:bbbbbbbb", 100.0)]).startswith(
        "blocker-settled:"
    )
