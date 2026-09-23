"""Deterministic elastic scaling of governed reviewer seats.

Elasticity here is capacity arithmetic over the governed reviewer profile
registry, not process management. The plan is a pure function of the set of
open review opportunities, the registry defaults, and Link's route state, so
two runs with identical inputs produce an identical plan. Nothing in this
module launches, claims, or mutates anything: it computes the exact plan Link
would act on, so reserved capacity, duplicate suppression, source-reviewer
separation, and bounded recovery are all deterministic and testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from skcapstone.fleet.reviewer_profiles import (
    ReviewerProfileError,
    ReviewerSeatProfile,
    load_reviewer_profiles,
    reviewer_seat_profiles,
)


def _parse_document_seat(identity: str, row: Mapping[str, Any]) -> ReviewerSeatProfile:
    """Parse one seat from an already-loaded registry document."""
    from skcapstone.fleet.reviewer_profiles import _parse_seat

    return _parse_seat(identity, row)


class ReviewerScalingError(ValueError):
    """The scaling inputs violate the deterministic elasticity contract."""


@dataclass(frozen=True)
class ReviewOpportunity:
    """One open review request that a reviewer seat could claim."""

    card_id: str
    generation: str
    source_head: str
    producer: str

    def __post_init__(self) -> None:
        if not self.card_id.strip():
            raise ReviewerScalingError("card_id must be a non-empty string")
        if not self.generation.strip():
            raise ReviewerScalingError("generation must be a non-empty string")


@dataclass(frozen=True)
class SeatClaim:
    """One deterministic reviewer seat to card generation assignment."""

    seat: str
    card_id: str
    generation: str
    reserved: bool


@dataclass(frozen=True)
class ScalingPlan:
    """The bounded, deterministic output of capacity planning."""

    claims: tuple[SeatClaim, ...]
    deferred: tuple[str, ...]
    duplicates_suppressed: tuple[str, ...]
    source_reviewers_excluded: tuple[str, ...]
    exhausted_seats: tuple[str, ...]


def load_registry(profile_source: object = None) -> dict[str, Any]:
    """Load the governed reviewer registry, honoring an explicit test path."""
    if profile_source is None:
        return load_reviewer_profiles()
    return load_reviewer_profiles(profile_source)  # type: ignore[arg-type]


def _registry_defaults(registry: Mapping[str, Any]) -> str:
    default_route = registry.get("default_model_route")
    if not isinstance(default_route, str) or not default_route.strip():
        raise ReviewerProfileError("default_model_route must be a non-empty string")
    escalation = registry.get("model_escalation_policy")
    if not isinstance(escalation, Mapping):
        raise ReviewerProfileError("model_escalation_policy must be an object")
    if escalation.get("automatic") is not False:
        raise ReviewerProfileError("model escalation must never be automatic")
    return default_route


def _seat_or_error(
    seats: Mapping[str, ReviewerSeatProfile], identity: str
) -> ReviewerSeatProfile:
    try:
        return seats[identity]
    except KeyError as exc:
        raise ReviewerScalingError(f"unknown reviewer seat {identity!r}") from exc


def _claim_block(seat: str, opportunity: ReviewOpportunity) -> SeatClaim:
    return SeatClaim(
        seat=seat,
        card_id=opportunity.card_id,
        generation=opportunity.generation,
        reserved=True,
    )


def build_scaling_plan(
    opportunities: Iterable[ReviewOpportunity],
    routes: Mapping[str, str],
    registry: Mapping[str, Any] | None = None,
    profile_source: object = None,
) -> ScalingPlan:
    """Build the deterministic elastic scaling plan for reviewer seats.

    Args:
        opportunities: Open review opportunities in Link's fresh feed order.
        routes: Card id to reviewer seat identity assignments Link made.
        registry: Optional pre-loaded registry document.
        profile_source: Optional explicit registry path for tests.

    Returns:
        A :class:`ScalingPlan` that is a pure function of the inputs.

    Raises:
        ReviewerScalingError: A route names an unknown seat, the routing seat
            appears as a reviewer, or opportunities repeat a card id.
    """
    document = registry if registry is not None else load_registry(profile_source)
    _registry_defaults(document)
    document_seats = document["seats"]
    seats = (
        reviewer_seat_profiles(profile_source)
        if registry is None
        else {
            identity: _parse_document_seat(identity, row)
            for identity, row in document_seats.items()
        }
    )
    reserved_capacity: Mapping[str, int] = document["reserved_capacity"]
    max_claims: int = document["recovery"]["max_claims_per_seat"]
    reviewing_order: tuple[str, ...] = tuple(document["reviewing_seats"])

    if not isinstance(routes, Mapping):
        raise ReviewerScalingError("routes must be a mapping of card id to reviewer seat")

    feed = list(opportunities)
    if len({opportunity.card_id for opportunity in feed}) != len(feed):
        raise ReviewerScalingError("duplicate card ids in the opportunity feed")

    for card_id, seat in routes.items():
        profile = _seat_or_error(seats, seat)
        if not profile.can_review():
            raise ReviewerScalingError(
                f"route for card {card_id} names seat {seat} which cannot review"
            )
        if seat == "link":
            raise ReviewerScalingError(
                "the routing seat link can never review the work it assigns"
            )

    claims: list[SeatClaim] = []
    suppressed: list[str] = []
    excluded: list[str] = []
    held: dict[str, int] = {identity: 0 for identity in reviewing_order}
    seen: set[str] = set()

    for opportunity in feed:
        if opportunity.card_id in seen:
            suppressed.append(opportunity.card_id)
            continue
        seen.add(opportunity.card_id)

        seat = routes.get(opportunity.card_id)
        if seat is not None:
            if opportunity.producer == seat:
                excluded.append(opportunity.card_id)
                continue
            if held[seat] >= min(reserved_capacity[seat], max_claims):
                excluded.append(opportunity.card_id)
                continue
            held[seat] += 1
            claims.append(_claim_block(seat, opportunity))
            continue

        for candidate in reviewing_order:
            if held[candidate] >= min(reserved_capacity[candidate], max_claims):
                continue
            if opportunity.producer == candidate:
                continue
            held[candidate] += 1
            claims.append(_claim_block(candidate, opportunity))
            break
        else:
            excluded.append(opportunity.card_id)

    exhausted = tuple(sorted(seat for seat, count in held.items() if count >= max_claims))
    producers = {opportunity.producer for opportunity in feed}
    return ScalingPlan(
        claims=tuple(claims),
        deferred=tuple(sorted(excluded)),
        duplicates_suppressed=tuple(suppressed),
        source_reviewers_excluded=tuple(
            sorted(
                card_id
                for card_id, seat in routes.items()
                if producers.issuperset({seat}) and any(
                    opportunity.card_id == card_id and opportunity.producer == seat
                    for opportunity in feed
                )
            )
        ),
        exhausted_seats=exhausted,
    )
