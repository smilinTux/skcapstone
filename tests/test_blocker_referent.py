"""A card is returnable only when every blocker it named has actually completed.

Fixtures use link_key/link_value, which is the shape the coordination store
really writes. An earlier version of a sibling module was tested against
key/value, passed every test, and found nothing at all on real data.
"""

import json

import pytest

from skcapstone.blocker_referent import (
    cited_referents,
    find_returnable,
    is_blocked_outcome,
    latest_blocked_verdicts,
)


def write_events(home, rows):
    events = home / "coordination" / "card_events"
    events.mkdir(parents=True, exist_ok=True)
    with open(events / "a.jsonl", "w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


# --- recognising a blocked outcome -------------------------------------------

@pytest.mark.parametrize("key", ["verdict", "outcome", "result", "final_verdict"])
def test_blocked_outcomes_are_recognised(key):
    assert is_blocked_outcome(key, "BLOCKED. blocked_on=card referent=card:c818148b")


def test_a_pass_is_not_a_blocked_outcome():
    assert not is_blocked_outcome("verdict", "PASS; PR #285; all checks green")


def test_a_non_outcome_link_is_ignored_even_when_it_says_blocked():
    assert not is_blocked_outcome("artifact", "BLOCKED. blocked_on=card referent=card:c818148b")


# --- extracting the referent, in the shapes workers actually write ------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("BLOCKED. blocked_on=card referent=card:c818148b", ["c818148b"]),
        ("BLOCKED blocked_on: card. Referent 04b218cd", ["04b218cd"]),
        ('BLOCKED {"blocked_on": {"referent": "card:2f9846b9"}}', ["2f9846b9"]),
        ("BLOCKED referent=card:2f9846b9 and referent=card:004aa32f", ["2f9846b9", "004aa32f"]),
    ],
)
def test_referents_are_extracted(text, expected):
    assert cited_referents(text) == expected


def test_an_acceptance_criterion_is_not_a_card_referent():
    """ac:1 names a criterion, not a card, and must never resolve to one."""
    assert cited_referents("BLOCKED. blocked_on=capability referent=ac:1") == []


# --- the latest verdict wins --------------------------------------------------

def test_a_card_that_later_passed_is_not_treated_as_blocked(tmp_path):
    write_events(tmp_path, [
        {"card_id": "aaa", "ts": "2026-08-01", "link_key": "verdict",
         "link_value": "BLOCKED. blocked_on=card referent=card:c818148b"},
        {"card_id": "aaa", "ts": "2026-08-02", "link_key": "verdict",
         "link_value": "PASS. Done."},
    ])
    assert latest_blocked_verdicts(tmp_path) == {}


def test_the_most_recent_blocked_verdict_is_the_one_used(tmp_path):
    write_events(tmp_path, [
        {"card_id": "aaa", "ts": "2026-08-01", "link_key": "verdict",
         "link_value": "BLOCKED. blocked_on=card referent=card:11111111"},
        {"card_id": "aaa", "ts": "2026-08-02", "link_key": "verdict",
         "link_value": "BLOCKED. blocked_on=card referent=card:22222222"},
    ])
    assert cited_referents(latest_blocked_verdicts(tmp_path)["aaa"]) == ["22222222"]


# --- the decision itself ------------------------------------------------------

def _sweep(tmp_path, done_map, open_ids=("aaa",)):
    return find_returnable(
        tmp_path,
        is_done=lambda p: done_map.get(p),
        is_open=lambda c: c in open_ids,
    )


def test_a_card_is_returned_when_its_blocker_completed(tmp_path):
    write_events(tmp_path, [
        {"card_id": "aaa", "ts": "1", "link_key": "verdict",
         "link_value": "BLOCKED. blocked_on=card referent=card:c818148b"},
    ])
    returnable, blocked, missing = _sweep(tmp_path, {"c818148b": True})
    assert returnable == ["aaa"] and blocked == 0 and missing == 0


def test_a_card_is_held_when_its_blocker_is_still_open(tmp_path):
    write_events(tmp_path, [
        {"card_id": "aaa", "ts": "1", "link_key": "verdict",
         "link_value": "BLOCKED. blocked_on=card referent=card:c818148b"},
    ])
    returnable, blocked, missing = _sweep(tmp_path, {"c818148b": False})
    assert returnable == [] and blocked == 1


def test_every_blocker_must_be_done_not_just_one(tmp_path):
    """The dangerous case: releasing a card while one of its blockers stands."""
    write_events(tmp_path, [
        {"card_id": "aaa", "ts": "1", "link_key": "verdict",
         "link_value": "BLOCKED referent=card:11111111 referent=card:22222222"},
    ])
    returnable, blocked, _ = _sweep(tmp_path, {"11111111": True, "22222222": False})
    assert returnable == [] and blocked == 1


def test_a_blocker_that_does_not_exist_is_counted_not_returned(tmp_path):
    write_events(tmp_path, [
        {"card_id": "aaa", "ts": "1", "link_key": "verdict",
         "link_value": "BLOCKED. blocked_on=card referent=card:deadbeef"},
    ])
    returnable, _, missing = _sweep(tmp_path, {"deadbeef": None})
    assert returnable == [] and missing == 1


def test_a_card_that_is_already_closed_is_left_alone(tmp_path):
    write_events(tmp_path, [
        {"card_id": "aaa", "ts": "1", "link_key": "verdict",
         "link_value": "BLOCKED. blocked_on=card referent=card:c818148b"},
    ])
    returnable, _, _ = _sweep(tmp_path, {"c818148b": True}, open_ids=())
    assert returnable == []


def test_the_legacy_key_value_layout_is_still_read(tmp_path):
    write_events(tmp_path, [
        {"card_id": "aaa", "ts": "1", "key": "verdict",
         "value": "BLOCKED. blocked_on=card referent=card:c818148b"},
    ])
    returnable, _, _ = _sweep(tmp_path, {"c818148b": True})
    assert returnable == ["aaa"]


# --- returning once per blocked verdict, not once per run ---------------------

def test_a_card_already_returned_for_this_verdict_is_not_returned_again(tmp_path):
    """The sweep must be safe to run on a timer.

    Labelling does not erase the historical BLOCKED verdict, so without this
    the same cards come back every single run and their backoff resets forever.
    """
    write_events(tmp_path, [
        {"card_id": "aaa", "ts": "2026-08-28T10:00:00+00:00", "link_key": "verdict",
         "link_value": "BLOCKED. blocked_on=card referent=card:c818148b"},
        {"card_id": "aaa", "ts": "2026-08-28T11:00:00+00:00",
         "action": "add_label", "label": "blocker-now-done"},
    ])
    returnable, _, _ = find_returnable(
        tmp_path, is_done=lambda p: True, is_open=lambda c: True
    )
    assert returnable == []


def test_a_card_blocked_again_after_being_returned_is_returned_again(tmp_path):
    """A NEW refusal after the label is a new situation and must be eligible."""
    write_events(tmp_path, [
        {"card_id": "aaa", "ts": "2026-08-28T10:00:00+00:00", "link_key": "verdict",
         "link_value": "BLOCKED. blocked_on=card referent=card:11111111"},
        {"card_id": "aaa", "ts": "2026-08-28T11:00:00+00:00",
         "action": "add_label", "label": "blocker-now-done"},
        {"card_id": "aaa", "ts": "2026-08-28T12:00:00+00:00", "link_key": "verdict",
         "link_value": "BLOCKED. blocked_on=card referent=card:22222222"},
    ])
    returnable, _, _ = find_returnable(
        tmp_path, is_done=lambda p: True, is_open=lambda c: True
    )
    assert returnable == ["aaa"]


def test_an_unrelated_label_does_not_suppress_a_return(tmp_path):
    write_events(tmp_path, [
        {"card_id": "aaa", "ts": "2026-08-28T10:00:00+00:00", "link_key": "verdict",
         "link_value": "BLOCKED. blocked_on=card referent=card:c818148b"},
        {"card_id": "aaa", "ts": "2026-08-28T11:00:00+00:00",
         "action": "add_label", "label": "deps-clear-rerun"},
    ])
    returnable, _, _ = find_returnable(
        tmp_path, is_done=lambda p: True, is_open=lambda c: True
    )
    assert returnable == ["aaa"]
