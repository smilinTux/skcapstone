"""A review must record a verdict before it can be completed.

The measurement behind this: of 317 completed review cards on 2026-08-28, 39 had
recorded no verdict. Some are permanently silent; others were a race, where the
worker completed the card and wrote the verdict afterwards. a93fd881 was observed
doing exactly that, reading complete-with-no-verdict and then PASS a minute later.
Requiring the verdict first removes the window.
"""

import json

import pytest

from skcapstone.review_verdict import (
    is_review_card,
    recorded_verdict,
    validate_review_completion,
)

_SUCCESSFUL_CI = (
    ("ci_check_docs", "SUCCESS", "2026-08-28T03:01:00"),
    ("ci_check_gitleaks", "SUCCESS", "2026-08-28T03:01:00"),
    ("ci_check_lint", "SUCCESS", "2026-08-28T03:01:00"),
    ("ci_check_shim_imports", "SUCCESS", "2026-08-28T03:01:00"),
    ("ci_check_python311", "SUCCESS", "2026-08-28T03:01:00"),
    ("ci_check_python312", "SUCCESS", "2026-08-28T03:01:00"),
)


def _home(tmp_path, card_id, title, links=()):
    card = tmp_path / "cards" / card_id
    card.mkdir(parents=True)
    (card / "core.json").write_text(json.dumps({"title": title}))
    ev = tmp_path / "coordination" / "card_events"
    ev.mkdir(parents=True)
    rows = [
        # the REAL shape the evidence store writes: link_key / link_value.
        # Fixtures using key/value passed while the module was broken.
        json.dumps(
            {"card_id": card_id, "action": "link", "link_key": k, "link_value": v, "ts": ts}
        )
        for k, v, ts in links
    ]
    (ev / "host.jsonl").write_text("\n".join(rows))
    return tmp_path


@pytest.mark.parametrize(
    "title",
    [
        "[SKW-X-01][S][REVIEW] Independently review the thing",
        "[REVIEW-119db735][S][REVIEW] Re-review",
        "[CARD-EVENT-SCHEMA-DESIGN-R1][REVIEW] Review provenance schema",
        "[SKW-X-01][S][REREVIEW] Independently rereview the thing",
        "[REREVIEW-119db735][S] Re-review",
    ],
)
def test_review_cards_are_recognised(title):
    assert is_review_card(title)


@pytest.mark.parametrize(
    "title",
    ["[SKGW-STRAT-06A][HUMAN] Approve cutover", "[FLEET-MON-01][P1] Restart monitors"],
)
def test_non_review_cards_are_left_alone(tmp_path, title):
    home = _home(tmp_path, "aaaaaaaa", title)
    assert not is_review_card(title)
    validate_review_completion("aaaaaaaa", title, home)


def test_completing_a_silent_review_is_refused(tmp_path):
    """The exact shape of a93fd881: claim, claim, complete, zero evidence rows."""
    home = _home(tmp_path, "a93fd881", "[REVIEW-bc69afd9][S][REVIEW] Independently review")
    with pytest.raises(ValueError) as err:
        validate_review_completion("a93fd881", "[REVIEW][S][REVIEW] x", home)
    assert "recorded no verdict" in str(err.value)


def test_terminal_pass_with_complete_ci_satisfies_it(tmp_path):
    home = _home(
        tmp_path,
        "bbbbbbbb",
        "[X][REVIEW] review",
        [("verdict", "PASS", "2026-08-28T03:00:00"), *_SUCCESSFUL_CI],
    )
    validate_review_completion("bbbbbbbb", "[X][REVIEW] review", home)


@pytest.mark.parametrize("verdict", ["FAIL", "BLOCKED blocked_on=card referent=inc-01"])
def test_negative_verdict_cannot_terminalize_governed_review(tmp_path, verdict):
    home = _home(
        tmp_path,
        "bbbbbbbb",
        "[X][REREVIEW] review",
        [("verdict", verdict, "2026-08-28T03:00:00")],
    )
    with pytest.raises(ValueError, match="canonical PASS"):
        validate_review_completion("bbbbbbbb", "[X][REREVIEW] review", home)


@pytest.mark.parametrize(
    "verdict",
    [
        "PASS_FOR_REVIEW",
        "PASS_FOR_REREVIEW",
        "PASS_BOGUS",
        "PASS extra",
        "PASSING",
        "UNKNOWN",
        "pass",
        "FAILURE",
        "BLOCKED",
        "BLOCKED blocked_on=card",
        "BLOCKED referent=inc-01",
    ],
)
def test_provisional_verdict_cannot_complete_review(tmp_path, verdict):
    home = _home(
        tmp_path,
        "bbbbbbbb",
        "[X][REVIEW] review",
        [("verdict", verdict, "2026-08-28T03:00:00")],
    )
    with pytest.raises(ValueError, match="nonterminal verdict"):
        validate_review_completion("bbbbbbbb", "[X][REVIEW] review", home)


def test_pending_required_check_blocks_terminal_pass(tmp_path):
    home = _home(
        tmp_path,
        "bbbbbbbb",
        "[X][REVIEW] review",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00"),
            *_SUCCESSFUL_CI,
            ("ci_check_python312", "pending", "2026-08-28T03:02:00"),
        ],
    )
    with pytest.raises(ValueError, match="not successful"):
        validate_review_completion("bbbbbbbb", "[X][REVIEW] review", home)


@pytest.mark.parametrize(
    "state",
    [
        "pending",
        "queued",
        "in_progress",
        "waiting",
        "missing",
        "cancelled",
        "skipped",
        "failure",
        "timed_out",
        "",
        "unknown",
    ],
)
def test_every_non_success_required_check_state_blocks_completion(tmp_path, state):
    home = _home(
        tmp_path,
        "bbbbbbbb",
        "[X][REVIEW] review",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00"),
            *_SUCCESSFUL_CI,
            ("ci_check_python312", state, "2026-08-28T03:02:00"),
        ],
    )
    with pytest.raises(ValueError, match="not successful"):
        validate_review_completion("bbbbbbbb", "[X][REVIEW] review", home)


@pytest.mark.parametrize("title", ["[X][REVIEW] review", "[X][REREVIEW] rereview"])
def test_exact_canonical_success_allows_completion(tmp_path, title):
    home = _home(
        tmp_path,
        "bbbbbbbb",
        title,
        [
            ("verdict", "PASS", "2026-08-28T03:00:00"),
            *_SUCCESSFUL_CI[:-1],
            ("ci_check_python312", "SUCCESS", "2026-08-28T03:02:00"),
        ],
    )
    validate_review_completion("bbbbbbbb", title, home)


@pytest.mark.parametrize("title", ["[X][REVIEW] review", "[X][REREVIEW] rereview"])
@pytest.mark.parametrize(
    "state",
    [
        "PASS",
        "PASSED",
        "SUCCESSFUL",
        "SUCCESS extra",
        "SUCCESSFUL_PREFIX",
        "success",
        "Success",
        " success ",
        "UNKNOWN",
        "",
        "IN_PROGRESS",
        "PENDING",
        "QUEUED",
        "WAITING",
        "FAILURE",
        "CANCELLED",
        "SKIPPED",
        "TIMED_OUT",
    ],
)
def test_noncanonical_required_check_states_fail_closed(tmp_path, title, state):
    home = _home(
        tmp_path,
        "bbbbbbbb",
        title,
        [
            ("verdict", "PASS", "2026-08-28T03:00:00"),
            *_SUCCESSFUL_CI[:-1],
            ("ci_check_python312", state, "2026-08-28T03:02:00"),
        ],
    )
    with pytest.raises(ValueError, match="ci_check_python312"):
        validate_review_completion("bbbbbbbb", title, home)


def test_later_green_check_allows_terminal_pass(tmp_path):
    home = _home(
        tmp_path,
        "bbbbbbbb",
        "[X][REVIEW] review",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00"),
            *_SUCCESSFUL_CI[:-1],
            ("ci_check_python312", "pending", "2026-08-28T03:01:00"),
            ("ci_check_python312", "SUCCESS", "2026-08-28T03:02:00"),
        ],
    )
    validate_review_completion("bbbbbbbb", "[X][REVIEW] review", home)


@pytest.mark.parametrize("title", ["[X][REVIEW] review", "[X][REREVIEW] rereview"])
def test_pass_without_required_ci_links_fails_closed(tmp_path, title):
    home = _home(
        tmp_path,
        "bbbbbbbb",
        title,
        [("verdict", "PASS", "2026-08-28T03:00:00")],
    )
    with pytest.raises(ValueError, match="not successful") as err:
        validate_review_completion("bbbbbbbb", title, home)
    assert "ci_check_python311" in str(err.value)
    assert "ci_check_python312" in str(err.value)


def test_pass_with_partial_required_ci_links_fails_closed(tmp_path):
    home = _home(
        tmp_path,
        "bbbbbbbb",
        "[X][REREVIEW] rereview",
        [("verdict", "PASS", "2026-08-28T03:00:00"), *_SUCCESSFUL_CI[:-1]],
    )
    with pytest.raises(ValueError, match="ci_check_python312"):
        validate_review_completion("bbbbbbbb", "[X][REREVIEW] rereview", home)


def test_an_empty_verdict_value_does_not_count(tmp_path):
    home = _home(
        tmp_path,
        "cccccccc",
        "[X][REVIEW] review",
        [("verdict", "   ", "2026-08-28T03:00:00")],
    )
    with pytest.raises(ValueError):
        validate_review_completion("cccccccc", "[X][REVIEW] review", home)


def test_a_non_outcome_link_does_not_count(tmp_path):
    """A PR link is not a judgement."""
    home = _home(
        tmp_path,
        "dddddddd",
        "[X][REVIEW] review",
        [("pr", "https://github.com/x/y/pull/1", "2026-08-28T03:00:00")],
    )
    with pytest.raises(ValueError):
        validate_review_completion("dddddddd", "[X][REVIEW] review", home)


def test_latest_verdict_is_returned(tmp_path):
    home = _home(
        tmp_path,
        "eeeeeeee",
        "[X][REVIEW] review",
        [
            ("verdict", "BLOCKED", "2026-08-28T01:00:00"),
            ("verdict", "PASS", "2026-08-28T02:00:00"),
        ],
    )
    assert recorded_verdict("eeeeeeee", home) == "PASS"
