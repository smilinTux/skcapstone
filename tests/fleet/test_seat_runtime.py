"""Tests for the one-shot Link, reviewer, and Mero runtime contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.seat_boundaries import Action, BoundaryError, require_authority
from skcapstone.seat_runtime import (
    MeroObservation,
    ReviewAssignmentRecommendation,
    append_review_launch_receipt,
    authorize_review_launch,
    recommend_reviewer,
    review_state_revision,
)

HASH = "a" * 64


def card(home: Path) -> None:
    """Create one unclaimed review card."""

    CardStore(home).create(
        CardCore(id="deadbeef", title="Source candidate", created_by="producer")
    )
    CardStore(home).create(
        CardCore(
            id="feedface",
            title="[REVIEW] Review candidate",
            created_by="producer",
            initial_labels=["review", "parent-deadbeef"],
        )
    )


def test_link_recommends_and_exact_reviewer_authorizes_fresh_assignment(
    tmp_path: Path,
) -> None:
    """The happy path is fenced to the exact distinct reviewer."""

    card(tmp_path)
    recommendation = recommend_reviewer(
        tmp_path,
        card_id="feedface",
        recommendation_id="assignment-1",
        author="producer",
        candidates=["producer", "link", "reviewer-one"],
        observed_process={"sessions": []},
        evidence_sha256=HASH,
    )
    assert CardStore(tmp_path).fold("feedface").owner is None
    handoff = authorize_review_launch(
        tmp_path,
        recommendation,
        actor="reviewer-one",
        current_process={"sessions": []},
        used_recommendation_ids=set(),
    )
    assert handoff.reviewer == "reviewer-one"
    assert handoff.card_id == "feedface"
    CardStore(tmp_path).append_event(
        "feedface",
        "claim",
        "reviewer-one",
        owner="reviewer-one",
        claim_revision="claim-revision-1",
    )
    receipt = append_review_launch_receipt(
        tmp_path,
        handoff,
        actor="reviewer-one",
        claim_revision="claim-revision-1",
        launched=True,
    )
    assert receipt["claim_revision"] == "claim-revision-1"
    assert receipt["launched"] is True


@pytest.mark.parametrize("candidate", ["", " ", "producer", "link"])
def test_link_rejects_non_distinct_reviewer(tmp_path: Path, candidate: str) -> None:
    """Blank, author, and Link identities never become reviewers."""

    card(tmp_path)
    with pytest.raises(BoundaryError, match="no distinct reviewer"):
        recommend_reviewer(
            tmp_path,
            card_id="feedface",
            recommendation_id="assignment-1",
            author="producer",
            candidates=[candidate],
            observed_process={"sessions": []},
            evidence_sha256=HASH,
        )


def test_reviewer_rejects_replay_and_state_drift(tmp_path: Path) -> None:
    """A recommendation is one-use and bound to the observed card state."""

    card(tmp_path)
    recommendation = recommend_reviewer(
        tmp_path,
        card_id="feedface",
        recommendation_id="assignment-1",
        author="producer",
        candidates=["reviewer-one"],
        observed_process={"sessions": []},
        evidence_sha256=HASH,
    )
    with pytest.raises(BoundaryError, match="replay"):
        authorize_review_launch(
            tmp_path,
            recommendation,
            actor="reviewer-one",
            current_process={"sessions": []},
            used_recommendation_ids={"assignment-1"},
        )
    CardStore(tmp_path).append_event("feedface", "add_label", "other", label="changed")
    with pytest.raises(BoundaryError, match="state changed"):
        authorize_review_launch(
            tmp_path,
            recommendation,
            actor="reviewer-one",
            current_process={"sessions": []},
            used_recommendation_ids=set(),
        )


def test_reviewer_rejects_process_drift(tmp_path: Path) -> None:
    """A same-card process appearing after Link's read denies launch."""

    card(tmp_path)
    recommendation = recommend_reviewer(
        tmp_path,
        card_id="feedface",
        recommendation_id="assignment-1",
        author="producer",
        candidates=["reviewer-one"],
        observed_process={"sessions": []},
        evidence_sha256=HASH,
    )
    with pytest.raises(BoundaryError, match="process changed"):
        authorize_review_launch(
            tmp_path,
            recommendation,
            actor="reviewer-one",
            current_process={"sessions": ["codex-auto-feedface"]},
            used_recommendation_ids=set(),
        )


def test_only_exact_reviewer_authorizes_launch(tmp_path: Path) -> None:
    """No coordinator, observer, producer, or unrelated worker can launch."""

    card(tmp_path)
    recommendation = recommend_reviewer(
        tmp_path,
        card_id="feedface",
        recommendation_id="assignment-1",
        author="producer",
        candidates=["reviewer-one"],
        observed_process={"sessions": []},
        evidence_sha256=HASH,
    )
    for actor in ("jarvis", "niobe", "link", "mero", "producer", "reviewer-two"):
        with pytest.raises(BoundaryError):
            authorize_review_launch(
                tmp_path,
                recommendation,
                actor=actor,
                current_process={"sessions": []},
                used_recommendation_ids=set(),
            )


def test_only_exact_claimed_reviewer_records_launch_receipt(tmp_path: Path) -> None:
    """A receipt is fenced to the recommended reviewer's exact claim."""

    card(tmp_path)
    recommendation = recommend_reviewer(
        tmp_path,
        card_id="feedface",
        recommendation_id="assignment-1",
        author="producer",
        candidates=["reviewer-one"],
        observed_process={"sessions": []},
        evidence_sha256=HASH,
    )
    handoff = authorize_review_launch(
        tmp_path,
        recommendation,
        actor="reviewer-one",
        current_process={"sessions": []},
        used_recommendation_ids=set(),
    )
    CardStore(tmp_path).append_event(
        "feedface",
        "claim",
        "reviewer-one",
        owner="reviewer-one",
        claim_revision="claim-revision-1",
    )
    for actor in ("jarvis", "niobe", "link", "mero", "producer", "reviewer-two"):
        with pytest.raises(BoundaryError):
            append_review_launch_receipt(
                tmp_path,
                handoff,
                actor=actor,
                claim_revision="claim-revision-1",
                launched=True,
            )
    receipt = append_review_launch_receipt(
        tmp_path,
        handoff,
        actor="reviewer-one",
        claim_revision="claim-revision-1",
        launched=True,
    )
    assert receipt["reviewer"] == "reviewer-one"


def test_unrecorded_recommendation_is_denied(tmp_path: Path) -> None:
    """A hand-built recommendation cannot bypass Link's recorded decision."""

    card(tmp_path)
    recommendation = ReviewAssignmentRecommendation(
        card_id="feedface",
        recommendation_id="assignment-forged",
        author="producer",
        reviewer="reviewer-one",
        observed_state_revision=review_state_revision(CardStore(tmp_path).fold("feedface")),
        observed_process={"sessions": []},
        evidence_sha256=HASH,
    )
    with pytest.raises(BoundaryError, match="not recorded exactly"):
        authorize_review_launch(
            tmp_path,
            recommendation,
            actor="reviewer-one",
            current_process={"sessions": []},
            used_recommendation_ids=set(),
        )


def test_generation_id_fails_closed_on_race_and_retries_cleanly(tmp_path: Path) -> None:
    card(tmp_path)
    store = CardStore(tmp_path)
    stale_revision = review_state_revision(store.fold("feedface"))
    store.append_event("feedface", "add_label", "other", label="qwen-suitable")

    with pytest.raises(BoundaryError, match="changed before recommendation"):
        recommend_reviewer(
            tmp_path,
            card_id="feedface",
            recommendation_id=None,
            author="producer",
            candidates=["reviewer-one"],
            observed_process={"sessions": []},
            evidence_sha256=HASH,
            expected_state_revision=stale_revision,
        )
    assert not any(
        event.get("action") == "review_assignment_recommendation"
        for event in store._read_events("feedface")
    )

    current_revision = review_state_revision(store.fold("feedface"))
    recommendation = recommend_reviewer(
        tmp_path,
        card_id="feedface",
        recommendation_id=None,
        author="producer",
        candidates=["reviewer-one"],
        observed_process={"sessions": []},
        evidence_sha256=HASH,
        expected_state_revision=current_revision,
    )
    repeated = recommend_reviewer(
        tmp_path,
        card_id="feedface",
        recommendation_id=None,
        author="producer",
        candidates=["reviewer-one"],
        observed_process={"sessions": []},
        evidence_sha256=HASH,
        expected_state_revision=current_revision,
    )
    events = [
        event
        for event in store._read_events("feedface")
        if event.get("action") == "review_assignment_recommendation"
    ]
    handoff = authorize_review_launch(
        tmp_path,
        repeated,
        actor="reviewer-one",
        current_process={"sessions": []},
        used_recommendation_ids=set(),
    )

    assert repeated.recommendation_id == recommendation.recommendation_id
    assert repeated.observed_state_revision == current_revision
    assert len(events) == 1
    assert events[0]["recommendation_id"] == recommendation.recommendation_id
    assert events[0]["observed_state_revision"] == current_revision
    assert handoff.recommendation_id == recommendation.recommendation_id


def test_assignment_denies_owned_review_card(tmp_path: Path) -> None:
    """A live owner appearing before recommendation fails closed."""

    card(tmp_path)
    CardStore(tmp_path).append_event(
        "feedface",
        "claim",
        "other-reviewer",
        owner="other-reviewer",
        claim_revision="claim-revision-1",
    )
    with pytest.raises(BoundaryError, match="unclaimed review work"):
        recommend_reviewer(
            tmp_path,
            card_id="feedface",
            recommendation_id="assignment-1",
            author="producer",
            candidates=["reviewer-one"],
            observed_process={"sessions": []},
            evidence_sha256=HASH,
        )


def test_mero_observes_without_mutating_card(tmp_path: Path) -> None:
    """Mero emits append-only evidence while lifecycle ownership stays unchanged."""

    card(tmp_path)
    before = CardStore(tmp_path).fold("feedface")
    MeroObservation(
        card_id="feedface",
        observation_id="observation-1",
        state="waiting",
        process={"session": None},
        evidence_sha256=HASH,
    ).append(tmp_path)
    after = CardStore(tmp_path).fold("feedface")
    assert (after.status, after.owner) == (before.status, before.owner)
    event = CardStore(tmp_path)._read_events("feedface")[-1]
    assert event["schema"] == "skfleet.mero-observation/v1"


def test_link_and_mero_cannot_mutate_fleet() -> None:
    """Negative authority remains enforced independently of runtime helpers."""

    for actor in ("link", "mero"):
        for action in (Action.CLAIM, Action.LAUNCH, Action.STOP, Action.REASSIGN):
            with pytest.raises(BoundaryError):
                require_authority(actor, action)


def test_assignment_rejects_blank_state_revision() -> None:
    """A hand-built recommendation without a state fence is invalid."""

    item = ReviewAssignmentRecommendation(
        card_id="feedface",
        recommendation_id="assignment-1",
        author="producer",
        reviewer="reviewer-one",
        observed_state_revision="",
        observed_process={"sessions": []},
        evidence_sha256=HASH,
    )
    with pytest.raises(BoundaryError, match="state revision"):
        item.validate()
