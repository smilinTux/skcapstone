"""Synced PASS projections must be identical before reseal can reuse them."""

import json

import pytest

from skcapstone.fleet import acceptance_reseal as recovery
from skcapstone.fleet import production_review_finish as finish
from skcapstone.historical_pr import history_revision
from tests.fleet.test_acceptance_reseal import (
    interrupted,  # noqa: F401
    reseal,
    retained_directory,  # noqa: F401
)
from tests.fleet.test_production_review_finish import pair  # noqa: F401
from tests.fleet.test_released_acceptance_reseal import released_history
from tests.fleet.test_source_bundle import source  # noqa: F401


def duplicate_projection(fixture, change=None):
    """Copy the exact PASS into a Syncthing conflict file, optionally corrupting it."""
    home, directory, context, store, *_ = fixture
    step = finish.read_json(directory / "step-08.intent.json")
    event = next(
        e
        for e in store._read_events(context["source"]["card"])
        if e.get("transition_id") == finish._digest(step)
    )
    projection = next(
        e
        for e in recovery._card_events(context["source"]["card"], home)
        if e.get("event_id") == event["event_id"]
    )
    if change:
        projection.update(change)
    path = home / "coordination/card_events/node.sync-conflict.jsonl"
    path.write_text(json.dumps(projection) + "\n")


@pytest.mark.host_systemd
@pytest.mark.parametrize(
    "interrupted", ["7907fdaf", "8f5077b2", "a4ed7c20", "add19bf8", "bb6da93e"], indirect=True
)
def test_identical_synced_pass_reseals_and_finishes_once(interrupted):  # noqa: F811
    fixture = released_history(interrupted)
    home, directory, context, store, *_ = fixture
    duplicate_projection(fixture)
    interrupted[2]["source_history"] = history_revision(store, context["source"]["card"])
    interrupted[2]["source_revision"] = finish.native_state(home, context["source"]["card"])[
        "revision"
    ]
    before = store._read_events(context["source"]["card"])
    result = reseal(interrupted)
    assert reseal(interrupted) == result
    assert finish.finish_pair(home, directory, context, guard=lambda: None)["accepted"]
    assert finish.finish_pair(home, directory, context, guard=lambda: None)["accepted"]
    after = store._read_events(context["source"]["card"])
    assert [e for e in after if e.get("link_key") == "verdict"] == [
        e for e in before if e.get("link_key") == "verdict"
    ]
    for role in ("source", "review"):
        assert (
            sum(e["action"] == "complete" for e in store._read_events(context[role]["card"])) == 1
        )


@pytest.mark.host_systemd
@pytest.mark.parametrize("change", [{"link_value": "BLOCKED"}, {"title": "foreign metadata"}])
def test_conflicting_synced_pass_refuses_without_writes(interrupted, change):  # noqa: F811
    fixture = released_history(interrupted)
    _, directory, context, store, *_ = fixture
    duplicate_projection(fixture, change)
    before = {role: store._read_events(context[role]["card"]) for role in ("source", "review")}
    with pytest.raises(ValueError, match="original PASS projection differs"):
        reseal(interrupted)
    assert not (directory / "reseal.json").exists()
    for role in before:
        assert store._read_events(context[role]["card"]) == before[role]
