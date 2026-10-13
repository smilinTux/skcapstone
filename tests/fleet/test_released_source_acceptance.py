"""Released-source finish accepts only unchanged content and exact release custody."""

import pytest

from skcapstone.fleet import production_review_finish as finish
from tests.fleet.test_production_review_finish import pair  # noqa: F401
from tests.fleet.test_source_bundle import source  # noqa: F401


def release(fixture):
    """Release the producer after the immutable acceptance context was captured."""
    home, directory, context, store, *_ = fixture
    item = context["source"]
    store.append_event(
        item["card"],
        "release_claim",
        "operator",
        released_owner=item["owner"],
        expected_claim_revision=item["claim"],
        transition_id="e" * 32,
        abandon_reason="not-abandoned",
    )
    return home, directory, context, store


@pytest.mark.host_systemd
@pytest.mark.parametrize(
    "change",
    [
        "claim",
        "assign",
        "unassign",
        "complete",
        "void",
        "head",
        "tree",
        "repository",
        "typed-head",
        "typed-tree",
        "wrong-owner",
        "wrong-claim",
        "verdict",
    ],
)
def test_released_source_with_later_custody_or_content_refuses_without_writes(
    pair, change  # noqa: F811
):
    home, directory, context, store = release(pair)
    item = context["source"]
    if change in {"wrong-owner", "wrong-claim"}:
        store.append_event(
            item["card"],
            "release_claim",
            "operator",
            released_owner="other" if change == "wrong-owner" else item["owner"],
            expected_claim_revision="f" * 32 if change == "wrong-claim" else item["claim"],
            transition_id="f" * 32,
        )
    elif change in {"claim", "assign", "unassign", "complete", "void"}:
        store.append_event(item["card"], change, "replacement", owner="replacement")
    elif change.startswith("typed-"):
        prior = next(e for e in store._read_events(item["card"]) if e["action"] == "verdict")
        payload = {k: v for k, v in prior.items() if k.startswith("candidate_")}
        payload["candidate_commit" if change == "typed-head" else "candidate_tree"] = "f" * 40
        store.append_event(
            item["card"],
            "verdict",
            item["owner"],
            verdict="PASS_FOR_REVIEW",
            expected_claim_revision=item["claim"],
            **payload,
        )
    else:
        store.append_event(
            item["card"],
            "link",
            "operator",
            link_key=change,
            link_value=(
                "https://example.invalid/other.git"
                if change == "repository"
                else "FAIL" if change == "verdict" else "f" * 40
            ),
        )
    before = {role: store._read_events(context[role]["card"]) for role in ("source", "review")}
    with pytest.raises(ValueError):
        finish.finish_pair(home, directory, context, guard=lambda: None)
    assert {role: store._read_events(context[role]["card"]) for role in before} == before


@pytest.mark.host_systemd
@pytest.mark.parametrize("lost", ["link", "review-complete", "source-complete"])
def test_released_finish_replays_persisted_revision_after_lost_reply(pair, lost):  # noqa: F811
    home, directory, context, store = release(pair)
    released_revision = finish.native_state(home, context["source"]["card"])["revision"]
    assert released_revision != context["source"]["revision"]
    crashed = []

    def command(home, args):
        result = finish.native_command(home, args)
        selected = (
            args[0]
            if args[0] == "link"
            else "review-complete" if args[1] == context["review"]["card"] else "source-complete"
        )
        if selected == lost and not crashed:
            crashed.append(True)
            raise OSError("lost reply")
        return result

    with pytest.raises(OSError, match="lost reply"):
        finish.finish_pair(home, directory, context, guard=lambda: None, command=command)
    assert (
        finish.read_json(directory / "finish-intent.json")["released_source_revision"]
        == released_revision
    )
    assert finish.finish_pair(home, directory, context, guard=lambda: None)["accepted"]
    for role in ("source", "review"):
        assert (
            len(
                [e for e in store._read_events(context[role]["card"]) if e["action"] == "complete"]
            )
            == 1
        )
