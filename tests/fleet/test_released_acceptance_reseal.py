"""Governed recovery of corrected, requalified and then released PASS histories."""

import pytest

from skcapstone.fleet import production_review_finish as finish
from skcapstone.historical_pr import history_revision
from tests.fleet.test_acceptance_reseal import (
    interrupted,  # noqa: F401
    reseal,
    retained_directory,  # noqa: F401
)  # noqa: F401
from tests.fleet.test_production_review_finish import pair  # noqa: F401
from tests.fleet.test_released_source_acceptance import release
from tests.fleet.test_source_bundle import source  # noqa: F401


def released_history(interrupted, requalified=True):  # noqa: F811
    """Capture fresh operator guards after exact release and trusted requalification."""
    fixture, _, guards = interrupted
    home, directory, context, store, _, receipt = fixture
    release(fixture)
    producer = context["source"]
    guards["source_revision"] = finish.native_state(home, producer["card"])["revision"]
    guards["source_history"] = history_revision(store, producer["card"])
    if requalified:
        prior = dict(receipt)
        receipt["receipt_sha256"] = "e" * 64
        finish.once(
            directory / "test-requalification.json",
            {
                "schema": "skfleet.acceptance-test-requalification/v1",
                "context_sha256": finish._digest(context),
                "finish_intent_sha256": finish._digest(
                    finish.read_json(directory / "finish-intent.json")
                ),
                "prior_test_receipt": prior,
                "current_test_receipt": dict(receipt),
            },
        )
    return fixture


@pytest.mark.host_systemd
@pytest.mark.parametrize(
    "interrupted", ["7907fdaf", "8f5077b2", "a4ed7c20", "add19bf8", "bb6da93e"], indirect=True
)
def test_five_released_histories_require_reseal_and_continue_once(interrupted):  # noqa: F811
    home, directory, context, store, *_ = released_history(interrupted)
    with pytest.raises(ValueError, match="guarded link replay changed"):
        finish.finish_pair(home, directory, context, guard=lambda: None)
    original = {r: store._read_events(context[r]["card"]) for r in ("source", "review")}
    result = reseal(interrupted)
    assert reseal(interrupted) == result
    assert result["released_custody"]["released_owner"] == context["source"]["owner"]
    assert "test-requalification.json" in result["files"]
    assert finish.finish_pair(home, directory, context, guard=lambda: None)["accepted"]
    assert finish.finish_pair(home, directory, context, guard=lambda: None)["accepted"]
    assert reseal(interrupted) == result
    for role in original:
        events = store._read_events(context[role]["card"])
        assert sum(e["action"] == "complete" for e in events) == 1
        assert [e for e in events if e.get("link_key") == "verdict"] == [
            e for e in original[role] if e.get("link_key") == "verdict"
        ]


@pytest.mark.host_systemd
@pytest.mark.parametrize("boundary", ["receipt", "native", "link", "complete"])
def test_released_requalified_recovery_survives_crashes(
    interrupted, monkeypatch, boundary  # noqa: F811
):
    from tests.fleet.test_acceptance_reseal import test_crash_recovery_and_lost_replies

    released_history(interrupted)
    test_crash_recovery_and_lost_replies(interrupted, monkeypatch, boundary)


@pytest.mark.host_systemd
@pytest.mark.parametrize("change", ["claim", "foreign", "tree", "outcome", "requalification"])
def test_released_reseal_refuses_changed_history_even_with_fresh_guards(
    interrupted, change  # noqa: F811
):
    home, directory, context, store, *_ = released_history(interrupted)
    card = context["source"]["card"]
    if change == "requalification":
        (directory / "test-requalification.json").write_text("{}")
    elif change == "claim":
        store.append_event(card, "claim", "other", owner="other", claim_revision="f" * 32)
    elif change == "foreign":
        store.append_event(card, "note", "other", note="changed history")
    else:
        store.append_event(
            card,
            "link",
            "other",
            link_key="verdict" if change == "outcome" else "tree",
            link_value="PASS" if change == "outcome" else "f" * 40,
        )
    guards = interrupted[2]
    guards["source_history"] = history_revision(store, card)
    guards["source_revision"] = finish.native_state(home, card)["revision"]
    before = store._read_events(card)
    with pytest.raises(ValueError):
        reseal(interrupted)
    assert store._read_events(card) == before
    assert not (directory / "reseal.json").exists()


@pytest.mark.host_systemd
def test_requalification_is_fenced_after_recovery_receipt(interrupted):  # noqa: F811
    home, directory, context, store, *_ = released_history(interrupted)
    reseal(interrupted)
    (directory / "test-requalification.json").write_text("{}")
    before = store._read_events(context["source"]["card"])
    with pytest.raises(ValueError, match="original journal changed"):
        finish.finish_pair(home, directory, context, guard=lambda: None)
    assert store._read_events(context["source"]["card"]) == before
