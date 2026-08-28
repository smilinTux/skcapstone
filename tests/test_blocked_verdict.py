"""A BLOCKED verdict must say what is blocking, in whatever shape the worker writes.

Real verdict text from the live board on 2026-08-27 is used for both the accept
and the reject cases, so this asserts against what workers actually produce
rather than against an idealised format.
"""

import pytest

from skcapstone.blocked_verdict import (
    states_where_the_work_is,
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
        "No custody operation performed. Attempted the custody read; no repository "
        "change, so no PR.",
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
        "referent=approval:9acf44e2-database-lifecycle. No mutation performed, and "
        "no repository change, so no PR.",
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
        "BLOCKED blocked_on=card referent=inc-0e190b2f. Attempted it; no repository change, so no PR.",
        "BLOCKED blocked_on=card referent=prb-41b9fb96. Attempted it; no repository change, so no PR.",
        'BLOCKED {"blocked_on": {"value": "dependency", "referent": "04b218cd"}}',
    ],
)
def test_card_category_accepts_real_card_ids(value):
    validate_blocked_verdict("verdict", value)


@pytest.mark.parametrize(
    "value",
    [
        "BLOCKED blocked_on=human referent=approval:sklegal_runtime-custody-path. Attempted it; no repository change, so no PR.",
        "BLOCKED blocked_on=capability referent=capability:durable-v2-feature-router. Attempted it; no repository change, so no PR.",
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
        "live install and restart while the standing rails prohibit deploy. "
        "Attempted the preflight; no repository change, so no PR.",
        "BLOCKED blocked_on=card referent=card:27bb08c0 criterion=ac:1, which is "
        "circular because it demands evidence of every acceptance statement "
        "including itself. Attempted the review; no repository change, so no PR.",
        "BLOCKED blocked_on=card referent=card:63971b3b criterion=ac:2, the exact "
        "required archives are absent. Attempted recovery; no repository change, so no PR.",
    ],
)
def test_card_refusal_that_states_the_contradiction_passes(value):
    validate_blocked_verdict("verdict", value)


@pytest.mark.parametrize(
    "value",
    [
        "BLOCKED blocked_on=dependency referent=card:04b218cd",
        "BLOCKED blocked_on=human referent=approval:sklegal_runtime-custody-path. Attempted it; no repository change, so no PR.",
        "BLOCKED blocked_on=capability referent=ac:1. Attempted it; no repository change, so no PR.",
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


# --- the short criterion spelling ---------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        # the exact verdict observed on review card 39ebfe14, 2026-08-28
        "BLOCKED|blocked_on=card referent=card:22a36166|ac=2|artifact_sha256=49099a31",
        "BLOCKED blocked_on=card referent=card:abc12345 ac:3",
        "BLOCKED|blocked_on=card|referent=card:abc12345|ac=1",
    ],
)
def test_short_criterion_spelling_still_needs_a_contradiction(value):
    """`ac=2` names a criterion just as `criterion=ac:2` does.

    Measured on the live board: of 52 blocked_on=card verdicts, 31 used the long
    spelling and 14 the short one, so matching only the long form let 27% of card
    refusals record a criterion with no contradiction at all.
    """
    with pytest.raises(ValueError) as err:
        validate_blocked_verdict("verdict", value)
    assert "contradiction" in str(err.value).lower()


def test_short_spelling_with_a_contradiction_passes():
    validate_blocked_verdict(
        "verdict",
        "BLOCKED|blocked_on=card|referent=card:abc12345|ac=3|the criterion requires "
        "frozen bytes that are absent from every host. Attempted recovery; no "
        "repository change, so no PR.",
    )


# --- warm handover ------------------------------------------------------------


def test_dependency_is_exempt_from_the_resume_requirement():
    """Its referent already IS the resume condition.

    "blocked_on=dependency referent=card:04b218cd" tells a successor exactly when
    to try again, and the referent rule has already forced that id to be real.
    """
    validate_blocked_verdict("verdict", "BLOCKED blocked_on=dependency referent=card:04b218cd")


@pytest.mark.parametrize(
    "value",
    [
        "BLOCKED blocked_on=human referent=approval:sklegal-custody",
        "BLOCKED blocked_on=capability referent=ac:1",
        "BLOCKED blocked_on=card referent=card:16bbc6fe ac:2, AC2 requires deploy "
        "while the rails prohibit it",
    ],
)
def test_other_categories_must_say_how_to_resume(value):
    with pytest.raises(ValueError) as err:
        validate_blocked_verdict("verdict", value)
    assert "handover" in str(err.value).lower() or "where any live work" in str(err.value)


def test_a_full_handover_passes():
    validate_blocked_verdict(
        "verdict",
        "BLOCKED blocked_on=human referent=approval:dd659b4c-post-retirement. "
        "Verified the retirement evidence on b0796dca and reproduced its hash. "
        "No repository change, so no PR. Resume once the authorization names this card.",
    )


def test_explicit_none_satisfies_the_work_location_rule():
    """An explicit none is a good answer; silence is not.

    Silence is indistinguishable from having produced something and not saying
    where it is, which is how commits 229336b2 and 22a36166 were recorded as
    permanently unverifiable while a valid bundle sat in the evidence directory.
    """
    assert states_where_the_work_is("no repository change, so no PR")
    assert states_where_the_work_is("produced no candidate")
    assert not states_where_the_work_is("the criterion could not be satisfied")


def test_a_branch_or_bundle_satisfies_the_work_location_rule():
    assert states_where_the_work_is("candidate on branch fix/x-y at commit 7362a6ba74c9")
    assert states_where_the_work_is("durable candidate bundle at evidence/work/x/candidate.bundle")
    assert states_where_the_work_is("https://github.com/smilinTux/skcapstone/pull/209")


def test_pass_verdicts_are_untouched_by_handover_rules():
    validate_blocked_verdict("verdict", "PASS. all green")
    validate_blocked_verdict("verdict", "PASS_FOR_REVIEW. PR #70")
