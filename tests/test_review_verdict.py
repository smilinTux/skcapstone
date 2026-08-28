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


@pytest.mark.parametrize(
    "verdict", ["PASS", "BLOCKED blocked_on=card referent=inc-01", "PASS_FOR_REVIEW"]
)
def test_any_recorded_verdict_satisfies_it(tmp_path, verdict):
    """This rule requires a verdict to exist. It does not judge the verdict."""
    home = _home(
        tmp_path,
        "bbbbbbbb",
        "[X][REVIEW] review",
        [("verdict", verdict, "2026-08-28T03:00:00")],
    )
    validate_review_completion("bbbbbbbb", "[X][REVIEW] review", home)


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
