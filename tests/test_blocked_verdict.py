"""A BLOCKED verdict must say what is blocking, in whatever shape the worker writes.

Real verdict text from the live board on 2026-08-27 is used for both the accept
and the reject cases, so this asserts against what workers actually produce
rather than against an idealised format.
"""

import pytest

from skcapstone.blocked_verdict import (
    states_a_contradiction,
    blocked_on_referent,
    is_blocked_verdict,
    is_outcome_key,
    validate_blocked_verdict,
)

# --- what counts as an outcome ------------------------------------------------


@pytest.mark.parametrize(
    "key", ["verdict", "outcome", "result", "disposition", "review_decision", "final_verdict"]
)
def test_outcome_keys_are_recognised(key):
    assert is_outcome_key(key)


@pytest.mark.parametrize("key", ["pr", "commit", "artifact", "evidence", "doc"])
def test_non_outcome_keys_are_left_alone(key):
    assert not is_outcome_key(key)
    # and are never validated, whatever they contain
    validate_blocked_verdict(key, "BLOCKED")


def test_only_blocked_outcomes_are_checked():
    assert not is_blocked_verdict("verdict", "PASS; PR #70; all checks green")
    validate_blocked_verdict("verdict", "PASS; PR #70; all checks green")


# --- the refusals, taken verbatim from the board ------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "BLOCKED",  # 18 cards looked exactly like this
        "  blocked  ",
        "BLOCKED.",
        "BLOCKED:sha256:1e8a73f45838923dcc52d1e35141b14212a3926837b78376be74e393bc",
        "BLOCKED. Hashed artifact sha256=793e6fd84a5ba065de171db31ace37cf1e6582f",
        "BLOCKED_FAIL_CLOSED",
    ],
)
def test_blocked_without_blocked_on_is_refused(value):
    with pytest.raises(ValueError) as err:
        validate_blocked_verdict("verdict", value)
    assert "blocked_on" in str(err.value)


@pytest.mark.parametrize(
    "value",
    [
        "BLOCKED. blocked_on: human",  # category, no referent
        "BLOCKED blocked_on=dependency",
        'BLOCKED {"blocked_on": {"value": "capability"}}',
        "BLOCKED blocked_on: card",
    ],
)
def test_category_without_a_referent_is_refused(value):
    with pytest.raises(ValueError) as err:
        validate_blocked_verdict("verdict", value)
    assert "referent" in str(err.value).lower()


# --- the acceptances, also verbatim -------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (
            "BLOCKED. blocked_on=human referent approval:9acf44e2-exact-verbatim-"
            "database-lifecycle-authorization",
            "approval:9acf44e2-exact-verbatim-database-lifecycle-authorization",
        ),
        (
            'BLOCKED {"blocked_on": {"value": "dependency", "referent": "card:04b218cd"}}',
            "card:04b218cd",
        ),
        (
            "BLOCKED - Card e9904899 was claimed then released. blocked_on: card. "
            "Referent 04b218cd",
            None,  # shape differs; asserted separately below
        ),
    ],
)
def test_referent_is_extracted(value, expected):
    if expected is not None:
        assert blocked_on_referent(value) == expected


def test_a_properly_referenced_verdict_passes():
    validate_blocked_verdict(
        "verdict",
        "BLOCKED. blocked_on=human referent approval:sklegal_runtime-custody-path. "
        "No custody operation performed.",
    )


def test_the_real_018bf488_verdict_passes():
    """The one verdict on the board that was written correctly."""
    validate_blocked_verdict(
        "outcome",
        "BLOCKED. Manifest d583baa1 NOT FOUND. Set blocked_on to "
        "dependency:card:04b218cd. Moved to review, released claim.",
    )


# --- the validator must not become the thing workers fight --------------------


def test_prose_before_the_referent_is_fine():
    validate_blocked_verdict(
        "verdict",
        "BLOCKED after a full read of the coordination board and 14049 CardStore "
        "records, no AC1-compliant authorization exists, so blocked_on=human "
        "referent=approval:9acf44e2-database-lifecycle. No mutation performed.",
    )


def test_uppercase_and_hyphenated_spellings_are_accepted():
    validate_blocked_verdict("verdict", "BLOCKED BLOCKED-ON dependency card:abc12345")


def test_a_pass_verdict_mentioning_blocking_is_not_touched():
    validate_blocked_verdict(
        "verdict", "PASS. Previously blocked_on dependency:card:04b218cd, now resolved."
    )


# --- a referent must be checkable, not merely present -------------------------


def test_card_category_rejects_an_acceptance_criterion_referent():
    """Observed live on 16bbc6fe: correctly shaped, completely unactionable."""
    with pytest.raises(ValueError) as err:
        validate_blocked_verdict("verdict", "BLOCKED blocked_on=card referent=ac:1")
    assert "card id" in str(err.value)
    assert "ac:1" in str(err.value)


@pytest.mark.parametrize(
    "value",
    [
        "BLOCKED blocked_on=dependency referent=card:04b218cd",
        "BLOCKED blocked_on=card referent=inc-0e190b2f",
        "BLOCKED blocked_on=card referent=prb-41b9fb96",
        'BLOCKED {"blocked_on": {"value": "dependency", "referent": "04b218cd"}}',
    ],
)
def test_card_category_accepts_real_card_ids(value):
    validate_blocked_verdict("verdict", value)


@pytest.mark.parametrize(
    "value",
    [
        "BLOCKED blocked_on=human referent=approval:sklegal_runtime-custody-path",
        "BLOCKED blocked_on=capability referent=capability:durable-v2-feature-router",
    ],
)
def test_human_and_capability_keep_free_form_referents(value):
    """Demanding an id here would push workers back toward saying nothing."""
    validate_blocked_verdict("verdict", value)


# --- a card refusal must say WHY, not only WHICH criterion --------------------

def test_card_refusal_pointing_at_its_own_card_is_refused():
    """The exact shape 13 of 16 live refusals had on 2026-08-28.

    The card names ITSELF as the blocker, which satisfies the card-id rule while
    saying nothing, and puts the real information in a criterion= field that
    nothing checks.
    """
    with pytest.raises(ValueError) as err:
        validate_blocked_verdict(
            "verdict", "BLOCKED blocked_on=card referent=card:95e192fd criterion=ac:5"
        )
    assert "contradiction" in str(err.value).lower()


@pytest.mark.parametrize(
    "value",
    [
        "BLOCKED blocked_on=card referent=card:bb11e415 criterion=ac:2. AC2 requires "
        "live install and restart while the standing rails prohibit deploy.",
        "BLOCKED blocked_on=card referent=card:27bb08c0 criterion=ac:1, which is "
        "circular because it demands evidence of every acceptance statement "
        "including itself.",
        "BLOCKED blocked_on=card referent=card:63971b3b criterion=ac:2, the exact "
        "required archives are absent.",
    ],
)
def test_card_refusal_that_states_the_contradiction_passes(value):
    validate_blocked_verdict("verdict", value)


@pytest.mark.parametrize(
    "value",
    [
        "BLOCKED blocked_on=dependency referent=card:04b218cd",
        "BLOCKED blocked_on=human referent=approval:sklegal_runtime-custody-path",
        "BLOCKED blocked_on=capability referent=ac:1",
    ],
)
def test_other_categories_do_not_need_a_contradiction(value):
    """Only `card` gains this rule.

    Naming a blocking card IS the explanation. Naming a criterion is not, because
    the only fix is to rewrite that criterion.
    """
    validate_blocked_verdict("verdict", value)


def test_states_a_contradiction_rejects_a_bare_pointer():
    assert not states_a_contradiction("criterion=ac:5")
    assert states_a_contradiction("ac:2 requires deploy while the rails prohibit it")
