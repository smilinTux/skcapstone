"""Fail-closed authority boundaries for the five operating seats."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Collection, Mapping

from .card_store import CardStore

if TYPE_CHECKING:
    from .link_merge_authority import MergeCandidate, MergeDecision


class BoundaryError(ValueError):
    """Raised when a seat attempts an action outside its authority."""


class Seat(StrEnum):
    """Canonical operating seats that participate in coordination."""

    JARVIS = "jarvis"
    LINK = "link"
    MERO = "mero"


class Action(StrEnum):
    """Actions whose seat ownership must remain explicit."""

    OBSERVE = "observe"
    RECOMMEND = "recommend"
    TRIAGE = "triage"
    ASSIGN_REVIEWER = "assign_reviewer"
    EVALUATE_MERGE = "evaluate_merge"
    MERGE = "merge"
    CLAIM = "claim"
    RELEASE = "release"
    LAUNCH = "launch"
    STOP = "stop"
    REASSIGN = "reassign"
    ROTATE = "rotate"
    REPAIR_WORKER = "repair_worker"
    DEPLOY = "deploy"
    ACTUATE_APPLICATION = "actuate_application"


_ALLOWED = {
    Seat.MERO: frozenset({Action.OBSERVE, Action.RECOMMEND}),
    Seat.LINK: frozenset(
        {
            Action.OBSERVE,
            Action.RECOMMEND,
            Action.TRIAGE,
            Action.ASSIGN_REVIEWER,
            Action.EVALUATE_MERGE,
            Action.MERGE,
        }
    ),
    Seat.JARVIS: frozenset(
        {
            Action.OBSERVE,
            Action.CLAIM,
            Action.RELEASE,
            Action.LAUNCH,
            Action.STOP,
            Action.REASSIGN,
            Action.ROTATE,
            Action.REPAIR_WORKER,
        }
    ),
}
_FLEET_MUTATIONS = frozenset(
    {
        Action.CLAIM,
        Action.RELEASE,
        Action.LAUNCH,
        Action.STOP,
        Action.REASSIGN,
        Action.ROTATE,
        Action.REPAIR_WORKER,
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


def require_authority(
    actor: str,
    action: Action,
    *,
    fenced_system_actors: Collection[str] = (),
) -> None:
    """Reject actions not owned by the named seat or fenced system actor."""

    normalized = actor.strip().lower()
    if action in _FLEET_MUTATIONS and normalized in fenced_system_actors:
        return
    try:
        seat = Seat(normalized)
    except ValueError as exc:
        raise BoundaryError(f"unknown or unfenced actor: {actor}") from exc
    if action not in _ALLOWED[seat]:
        raise BoundaryError(f"{seat.value} is not authorized for {action.value}")


def assign_distinct_reviewer(*, author: str, assigner: str, candidates: Collection[str]) -> str:
    """Choose the first stable reviewer distinct from author and Link."""

    require_authority(assigner, Action.ASSIGN_REVIEWER)
    excluded = {author.strip().lower(), assigner.strip().lower()}
    for candidate in candidates:
        normalized = candidate.strip()
        if normalized and normalized.lower() not in excluded:
            return normalized
    raise BoundaryError("no distinct reviewer is available")


@dataclass(frozen=True)
class DispatchRecommendation:
    """Advisory observation that only Jarvis may evaluate and act upon."""

    card_id: str
    recommendation_id: str
    recommender: str
    observed_at: datetime
    observed_claim_owner: str | None
    observed_claim_revision: str | None
    observed_process: Mapping[str, object]
    reason: str
    evidence_sha256: str

    def validate(self) -> None:
        """Validate the closed recommendation envelope."""

        require_authority(self.recommender, Action.RECOMMEND)
        required = (self.card_id, self.recommendation_id, self.reason)
        if any(not value.strip() for value in required):
            raise BoundaryError("recommendation identifiers and reason are required")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise BoundaryError("observed_at must be timezone aware")
        if not _SHA256.fullmatch(self.evidence_sha256):
            raise BoundaryError("evidence_sha256 must be lowercase SHA-256")

    def as_event(self) -> dict[str, object]:
        """Return the typed payload for append-only CardStore emission."""

        self.validate()
        return {
            "schema": "skfleet.dispatch-recommendation/v1",
            "card_id": self.card_id,
            "recommendation_id": self.recommendation_id,
            "recommender": self.recommender,
            "observed_at": self.observed_at.isoformat(),
            "observed_claim_owner": self.observed_claim_owner,
            "observed_claim_revision": self.observed_claim_revision,
            "observed_process": dict(self.observed_process),
            "reason": self.reason,
            "evidence_sha256": self.evidence_sha256,
        }


def append_recommendation(home: Path, recommendation: DispatchRecommendation) -> dict[str, object]:
    """Append one typed recommendation idempotently to its card event log."""

    payload = recommendation.as_event()
    payload.pop("card_id")
    payload.pop("recommender")
    return CardStore(home).append_event(
        recommendation.card_id,
        "dispatch_recommendation",
        recommendation.recommender,
        transition_id=recommendation.recommendation_id,
        **payload,
    )


def evaluate_merge_as_link(actor: str, candidate: MergeCandidate) -> MergeDecision:
    """Evaluate exact-head merge eligibility only for the Link seat."""

    require_authority(actor, Action.EVALUATE_MERGE)
    if actor.strip().lower() != Seat.LINK:
        raise BoundaryError("only link may evaluate the merge queue")
    from .link_merge_authority import evaluate_link_merge

    return evaluate_link_merge(candidate)


def authorize_recommendation_action(
    recommendation: DispatchRecommendation,
    *,
    actor: str,
    action: Action,
    current_claim_owner: str | None,
    current_claim_revision: str | None,
    current_process: Mapping[str, object],
    used_recommendation_ids: Collection[str],
) -> None:
    """Fence Jarvis action against replay and stale CardStore/process state."""

    recommendation.validate()
    require_authority(actor, action)
    if actor.strip().lower() != Seat.JARVIS:
        raise BoundaryError("only jarvis may act on a recommendation")
    if recommendation.recommendation_id in used_recommendation_ids:
        raise BoundaryError("recommendation replay denied")
    if recommendation.observed_claim_owner != current_claim_owner:
        raise BoundaryError("claim owner changed after observation")
    if (
        not recommendation.observed_claim_revision
        or not recommendation.observed_claim_revision.strip()
    ):
        raise BoundaryError("observed claim revision is required")
    if not current_claim_revision or not current_claim_revision.strip():
        raise BoundaryError("current claim revision is required")
    if recommendation.observed_claim_revision != current_claim_revision:
        raise BoundaryError("claim revision changed after observation")
    if dict(recommendation.observed_process) != dict(current_process):
        raise BoundaryError("process state changed after observation")


def utc_now() -> datetime:
    """Return an aware UTC timestamp for recommendation producers."""

    return datetime.now(timezone.utc)
