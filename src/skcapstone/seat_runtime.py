"""One-shot runtime contracts for Link, Jarvis, and Mero."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .card_store import Card, CardStore
from .seat_boundaries import Action, BoundaryError, assign_distinct_reviewer, require_authority

_SHA256 = re.compile(r"[0-9a-f]{64}")


def review_state_revision(card: Card) -> str:
    """Hash the fields whose change invalidates a review assignment."""

    payload = {
        "id": card.id,
        "status": str(card.status),
        "owner": card.owner,
        "labels": sorted(card.labels),
        "dependencies": sorted(card.dependencies),
        "title": card.title,
        "description": card.description,
        "acceptance_criteria": card.acceptance_criteria,
        "links": card.links,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ReviewAssignmentRecommendation:
    """Link advice that Jarvis may turn into one exact review launch."""

    card_id: str
    recommendation_id: str
    author: str
    reviewer: str
    observed_state_revision: str
    observed_process: Mapping[str, object]
    evidence_sha256: str
    recommender: str = "link"

    def validate(self) -> None:
        """Reject incomplete, non-Link, or non-independent assignments."""

        require_authority(self.recommender, Action.ASSIGN_REVIEWER)
        if self.recommender.strip().lower() != "link":
            raise BoundaryError("only link may recommend a reviewer")
        required = (self.card_id, self.recommendation_id, self.author, self.reviewer)
        if any(not value.strip() for value in required):
            raise BoundaryError("assignment identifiers are required")
        assigned = assign_distinct_reviewer(
            author=self.author,
            assigner=self.recommender,
            candidates=[self.reviewer],
        )
        if assigned != self.reviewer.strip():
            raise BoundaryError("reviewer identity is not normalized")
        if not _SHA256.fullmatch(self.observed_state_revision):
            raise BoundaryError("observed state revision must be lowercase SHA-256")
        if not _SHA256.fullmatch(self.evidence_sha256):
            raise BoundaryError("evidence_sha256 must be lowercase SHA-256")

    def as_event(self) -> dict[str, object]:
        """Return the closed append-only event payload."""

        self.validate()
        return {
            "schema": "skfleet.review-assignment/v1",
            "recommendation_id": self.recommendation_id,
            "author": self.author,
            "reviewer": self.reviewer,
            "observed_state_revision": self.observed_state_revision,
            "observed_process": dict(self.observed_process),
            "evidence_sha256": self.evidence_sha256,
        }


def recommend_reviewer(
    home: Path,
    *,
    card_id: str,
    recommendation_id: str | None,
    author: str,
    candidates: list[str],
    observed_process: Mapping[str, object],
    evidence_sha256: str,
    expected_state_revision: str | None = None,
) -> ReviewAssignmentRecommendation:
    """Have Link append one revision-bound, distinct-reviewer recommendation."""

    store = CardStore(home)
    card = store.fold(card_id)
    if (
        card is None
        or card.owner is not None
        or getattr(card.status, "value", card.status) != "ready"
    ):
        raise BoundaryError("review card is not unclaimed review work in Ready")
    labels = {str(label).lower() for label in card.labels}
    if not {"review", "independent-review"} <= labels:
        raise BoundaryError("review card lacks independent review labels")
    state_revision = review_state_revision(card)
    if expected_state_revision is not None and expected_state_revision != state_revision:
        raise BoundaryError("review card state changed before recommendation")
    reviewer = assign_distinct_reviewer(author=author, assigner="link", candidates=candidates)
    if recommendation_id is None:
        recommendation_id = (
            "link-review-"
            + hashlib.sha256(
                (
                    card_id + "\0" + reviewer + "\0" + evidence_sha256 + "\0" + state_revision
                ).encode()
            ).hexdigest()[:32]
        )
    recommendation = ReviewAssignmentRecommendation(
        card_id=card_id,
        recommendation_id=recommendation_id,
        author=author.strip(),
        reviewer=reviewer,
        observed_state_revision=state_revision,
        observed_process=dict(observed_process),
        evidence_sha256=evidence_sha256,
    )
    store.append_event(
        card_id,
        "review_assignment_recommendation",
        "link",
        transition_id=recommendation_id,
        **recommendation.as_event(),
    )
    return recommendation


@dataclass(frozen=True)
class ReviewLaunchHandoff:
    """Exact input one governed reviewer may pass to the claim path."""

    card_id: str
    reviewer: str
    recommendation_id: str
    state_revision: str


def authorize_review_launch(
    home: Path,
    recommendation: ReviewAssignmentRecommendation,
    *,
    actor: str,
    current_process: Mapping[str, object],
    used_recommendation_ids: set[str],
) -> ReviewLaunchHandoff:
    """Validate one recorded Link recommendation for its exact reviewer."""

    recommendation.validate()
    if actor.strip() != recommendation.reviewer:
        raise BoundaryError("only the exact recommended reviewer may launch")
    require_authority(actor, Action.LAUNCH, fenced_system_actors={recommendation.reviewer.lower()})
    if recommendation.recommendation_id in used_recommendation_ids:
        raise BoundaryError("recommendation replay denied")
    store = CardStore(home)
    recorded = any(
        event.get("action") == "review_assignment_recommendation"
        and event.get("writer") == "link"
        and all(event.get(key) == value for key, value in recommendation.as_event().items())
        for event in store._read_events(recommendation.card_id)
    )
    if not recorded:
        raise BoundaryError("review recommendation is not recorded exactly")
    card = store.fold(recommendation.card_id)
    if (
        card is None
        or card.owner is not None
        or getattr(card.status, "value", card.status) != "ready"
    ):
        raise BoundaryError("review card is no longer unclaimed review work in Ready")
    current_revision = review_state_revision(card)
    if current_revision != recommendation.observed_state_revision:
        raise BoundaryError("review card state changed after assignment")
    if dict(current_process) != dict(recommendation.observed_process):
        raise BoundaryError("review card process changed after assignment")
    return ReviewLaunchHandoff(
        card_id=recommendation.card_id,
        reviewer=recommendation.reviewer,
        recommendation_id=recommendation.recommendation_id,
        state_revision=current_revision,
    )


def append_review_launch_receipt(
    home: Path,
    handoff: ReviewLaunchHandoff,
    *,
    actor: str,
    claim_revision: str,
    launched: bool,
) -> dict[str, object]:
    """Record one governed reviewer's exact claim generation and result."""

    if actor.strip() != handoff.reviewer:
        raise BoundaryError("only the exact recommended reviewer may record launch")
    require_authority(actor, Action.LAUNCH, fenced_system_actors={handoff.reviewer.lower()})
    if not claim_revision.strip():
        raise BoundaryError("claim revision is required")
    store = CardStore(home)
    card = store.fold(handoff.card_id)
    if (
        card is None
        or card.owner != handoff.reviewer
        or card.meta.get("_claim_revision") != claim_revision
    ):
        raise BoundaryError("review launch receipt does not match the exact claim")
    return store.append_event(
        handoff.card_id,
        "review_assignment_launch",
        actor,
        transition_id=(actor + "-" + handoff.recommendation_id + "-" + claim_revision),
        schema="skfleet.review-assignment-launch/v1",
        recommendation_id=handoff.recommendation_id,
        reviewer=handoff.reviewer,
        observed_state_revision=handoff.state_revision,
        claim_revision=claim_revision,
        launched=bool(launched),
    )


@dataclass(frozen=True)
class MeroObservation:
    """Read-only worker observation emitted by Mero."""

    card_id: str
    observation_id: str
    state: str
    process: Mapping[str, object]
    evidence_sha256: str

    def append(self, home: Path) -> dict[str, object]:
        """Append the observation without changing lifecycle or runtime state."""

        require_authority("mero", Action.OBSERVE)
        if not self.card_id.strip() or not self.observation_id.strip() or not self.state.strip():
            raise BoundaryError("observation identifiers and state are required")
        if not _SHA256.fullmatch(self.evidence_sha256):
            raise BoundaryError("evidence_sha256 must be lowercase SHA-256")
        return CardStore(home).append_event(
            self.card_id,
            "mero_observation",
            "mero",
            transition_id=self.observation_id,
            schema="skfleet.mero-observation/v1",
            state=self.state,
            process=dict(self.process),
            evidence_sha256=self.evidence_sha256,
        )
