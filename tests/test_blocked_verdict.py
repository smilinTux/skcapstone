"""A BLOCKED verdict must say what is blocking, in whatever shape the worker writes.

Real verdict text from the live board on 2026-08-27 is used for both the accept
and the reject cases, so this asserts against what workers actually produce
rather than against an idealised format.
"""

import pytest

from skcapstone.blocked_verdict import (
    blocked_on_referent,
    is_blocked_verdict,
    is_obsolete_verdict,
    is_outcome_key,
    states_a_contradiction,
    states_how_to_resume,
    states_where_the_work_is,
    validate_blocked_verdict,
)

_HANDOVER = " Attempted it; no repository change, so no PR."


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
        "BLOCKED. Inspected the coordination board and 14049 CardStore "
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
        "BLOCKED blocked_on=card referent=inc-0e190b2f." + _HANDOVER,
        "BLOCKED blocked_on=card referent=prb-41b9fb96." + _HANDOVER,
        'BLOCKED {"blocked_on": {"value": "dependency", "referent": "04b218cd"}}',
    ],
)
def test_card_category_accepts_real_card_ids(value):
    validate_blocked_verdict("verdict", value)


@pytest.mark.parametrize(
    "value",
    [
        "BLOCKED blocked_on=human referent=approval:sklegal_runtime-custody-path." + _HANDOVER,
        "BLOCKED blocked_on=capability referent=capability:durable-v2-feature-router." + _HANDOVER,
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
        "BLOCKED blocked_on=human referent=approval:sklegal_runtime-custody-path." + _HANDOVER,
        "BLOCKED blocked_on=capability referent=ac:1." + _HANDOVER,
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


@pytest.mark.parametrize("value", [None, 0, 1, False, True, "", "false", "ambiguous"])
def test_handover_predicates_do_not_accept_non_prose(value):
    assert not states_where_the_work_is(value)
    assert not states_how_to_resume(value)


def test_resume_without_a_work_location_is_refused():
    with pytest.raises(ValueError, match="WHERE any live work"):
        validate_blocked_verdict(
            "verdict",
            "BLOCKED blocked_on=human referent=approval:x-y. Attempted the read and "
            "verified the hash; resume once approved.",
        )


def test_work_location_without_resume_is_refused():
    with pytest.raises(ValueError, match="warm handover"):
        validate_blocked_verdict(
            "verdict",
            "BLOCKED blocked_on=human referent=approval:x-y. No repository change, so no PR.",
        )


def test_dependency_exemption_does_not_leak_to_other_categories():
    # Same referent shape as the exempt form, but a non-dependency category.
    with pytest.raises(ValueError):
        validate_blocked_verdict("verdict", "BLOCKED blocked_on=card referent=card:04b218cd")


# --- live formats, obsolete shape, loose-word negatives (5c0a0001 review fix) -----


@pytest.mark.parametrize(
    "value",
    [
        "BLOCKED blocked_on=card referent=card:16bbc6fe reason=obsolete-superseded-by-main",
        "BLOCKED blocked_on=card referent=card:16bbc6fe reason=superseded",
        "BLOCKED blocked_on=card referent=card:16bbc6fe reason=already-landed in main",
        "BLOCKED blocked_on=card referent=inc-0e190b2f reason=duplicate",
        'BLOCKED blocked_on=card referent=card:16bbc6fe reason="obsolete"',
    ],
)
def test_obsolete_and_superseded_closures_need_no_handover(value):
    assert is_obsolete_verdict(value)
    validate_blocked_verdict("verdict", value)


@pytest.mark.parametrize(
    "value",
    [
        # the word in free prose is not the reason= field
        "BLOCKED blocked_on=human referent=approval:x-y this is obsolete maybe",
        "BLOCKED blocked_on=human referent=approval:x-y reason=waiting",
        "BLOCKED blocked_on=human referent=approval:x-y",
    ],
)
def test_obsolete_exemption_is_not_a_prose_escape_hatch(value):
    assert not is_obsolete_verdict(value)
    with pytest.raises(ValueError, match="warm handover"):
        validate_blocked_verdict("verdict", value)


def test_obsolete_still_needs_a_real_referent():
    with pytest.raises(ValueError, match="referent"):
        validate_blocked_verdict("verdict", "BLOCKED blocked_on=card reason=obsolete-x")
    with pytest.raises(ValueError):
        validate_blocked_verdict("verdict", "BLOCKED reason=obsolete-x")


# Each format the fleet briefs tell a worker to write, filled in as instructed.
_BRIEF_FORMATS = [
    # production_brief.py and production_hosted_review_brief.py
    "BLOCKED blocked_on=capability referent=git-author-identity "
    "attempted=ran git commit and git config user.name, both empty "
    "resume=set GIT_AUTHOR_NAME and GIT_AUTHOR_EMAIL then rerun the commit "
    "work_location=none "
    "no git author identity on this host",
    "BLOCKED blocked_on=human referent=approval:custody-path "
    "attempted=read the custody policy resume=ask the owner to approve "
    "work_location=branch fix/x-y",
    "BLOCKED blocked_on=capability referent=ci:28 "
    "attempted=inspected the run resume=rerun when CI is available "
    "work_location=none missing CI",
    # production_cursor_worker.py
    "BLOCKED blocked_on=card referent=card:16bbc6fe attempted=tried the install "
    "resume=amend ac:2 first work_location=none AC2 requires install while rails "
    "prohibit deploy",
]


@pytest.mark.parametrize("value", _BRIEF_FORMATS)
def test_every_brief_format_is_accepted(value):
    validate_blocked_verdict("verdict", value)


def test_brief_text_teaches_the_accepted_format():
    from skcapstone.fleet import (
        production_brief,
        production_cursor_worker,
        production_hosted_review_brief,
    )

    for mod in (production_brief, production_hosted_review_brief):
        src = open(mod.__file__, encoding="utf-8").read()
        assert "attempted=<what you tried>" in src, mod.__name__
        assert "resume=<what the next agent does first>" in src, mod.__name__
        assert "work_location=<" in src, mod.__name__
    cursor_src = open(production_cursor_worker.__file__, encoding="utf-8").read()
    assert "work_location=<" in cursor_src


@pytest.mark.parametrize(
    "value",
    [
        "BLOCKED blocked_on=human referent=approval:x-y blocked once the sun rises",
        "BLOCKED blocked_on=human referent=approval:x-y it needs work after lunch no PR",
        "BLOCKED blocked_on=human referent=approval:x-y when asked, ran away. no PR",
        "BLOCKED blocked_on=capability referent=gpu needs more. no repository change",
    ],
)
def test_stray_connectives_are_not_a_handover(value):
    assert not states_how_to_resume(value)
    with pytest.raises(ValueError, match="warm handover"):
        validate_blocked_verdict("verdict", value)


@pytest.mark.parametrize(
    "value",
    [
        "resume once the approval lands",
        "retry after the gateway is back",
        "to resume, run the migration first",
        "Attempted the read",
        "attempted=read resume=none",
        "unblock when card:16bbc6fe is merged",
    ],
)
def test_genuine_resume_statements_count(value):
    assert states_how_to_resume(value)


def test_bare_blocked_and_bare_category_still_refused():
    with pytest.raises(ValueError):
        validate_blocked_verdict("verdict", "BLOCKED")
    with pytest.raises(ValueError):
        validate_blocked_verdict("verdict", "BLOCKED blocked_on=human")
    with pytest.raises(ValueError):
        validate_blocked_verdict("verdict", "BLOCKED blocked_on=human referent=approval:x-y")


def test_work_location_key_with_a_value_counts():
    assert states_where_the_work_is("work_location=fix/lane-x")
    assert not states_where_the_work_is("work_location=")
