"""Tests for the one-shot Link, Jarvis, and Mero runtime contracts."""

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


def test_link_recommends_and_jarvis_authorizes_fresh_assignment(tmp_path: Path) -> None:
    """The happy path stays advisory until Jarvis authorizes it."""

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
        actor="jarvis",
        current_process={"sessions": []},
        used_recommendation_ids=set(),
    )
    assert handoff.reviewer == "reviewer-one"
    assert handoff.card_id == "feedface"
    receipt = append_review_launch_receipt(
        tmp_path,
        handoff,
        actor="jarvis",
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


def test_jarvis_rejects_replay_and_state_drift(tmp_path: Path) -> None:
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
            actor="jarvis",
            current_process={"sessions": []},
            used_recommendation_ids={"assignment-1"},
        )
    CardStore(tmp_path).append_event("feedface", "add_label", "other", label="changed")
    with pytest.raises(BoundaryError, match="state changed"):
        authorize_review_launch(
            tmp_path,
            recommendation,
            actor="jarvis",
            current_process={"sessions": []},
            used_recommendation_ids=set(),
        )


def test_jarvis_rejects_process_drift(tmp_path: Path) -> None:
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
            actor="jarvis",
            current_process={"sessions": ["codex-auto-feedface"]},
            used_recommendation_ids=set(),
        )


def test_only_jarvis_authorizes_launch(tmp_path: Path) -> None:
    """Link and Mero cannot cross into fleet launch authority."""

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
    for actor in ("link", "mero"):
        with pytest.raises(BoundaryError):
            authorize_review_launch(
                tmp_path,
                recommendation,
                actor=actor,
                current_process={"sessions": []},
                used_recommendation_ids=set(),
            )


def test_only_jarvis_records_launch_receipt(tmp_path: Path) -> None:
    """Link and Mero cannot record a fleet launch outcome."""

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
        actor="jarvis",
        current_process={"sessions": []},
        used_recommendation_ids=set(),
    )
    for actor in ("link", "mero"):
        with pytest.raises(BoundaryError):
            append_review_launch_receipt(
                tmp_path,
                handoff,
                actor=actor,
                claim_revision="claim-revision-1",
                launched=True,
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
