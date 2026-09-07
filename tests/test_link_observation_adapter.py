import pytest

from skcapstone.link_cycle import (
    MediatedObservationFeed, PullRequestObservation, ReviewerIdentity,
    consume_observation_feed,
)
from skcapstone.seat_boundaries import BoundaryError


def observation():
    return PullRequestObservation(
        "org/repo", 1, "title", "author", "a" * 40, "b" * 40,
        "success", "clean", (), "card", "generation", "2026-09-06T14:00:00Z",
    )


def feed():
    return MediatedObservationFeed.build(
        source_revision="c" * 64, observed_at="2026-09-06T14:00:00Z",
        observations=[observation()],
        reviewers=[ReviewerIdentity("reviewer", "id", "host", "session", "workspace")],
    )


def test_feed_supplies_exact_inputs_and_hashes():
    item = feed()
    observations, reviewers, revision, evidence = consume_observation_feed(
        item, now="2026-09-06T14:02:00Z"
    )
    assert observations == (observation(),)
    assert reviewers[0].name == "reviewer"
    assert revision == "c" * 64
    assert evidence == item.feed_hash


@pytest.mark.parametrize("change", ["evidence_sha256", "schema"])
def test_malformed_feed_is_bounded(change):
    item = feed()
    value = "0" * 64 if change == "evidence_sha256" else "wrong"
    broken = __import__("dataclasses").replace(item, **{change: value})
    with pytest.raises(BoundaryError):
        consume_observation_feed(broken, now="2026-09-06T14:02:00Z")


def test_stale_and_replayed_feed_are_rejected():
    item = feed()
    with pytest.raises(BoundaryError):
        consume_observation_feed(item, now="2026-09-06T20:00:00Z")
    with pytest.raises(BoundaryError):
        consume_observation_feed(item, now="2026-09-06T14:02:00Z", prior_feed_hash=item.feed_hash)
