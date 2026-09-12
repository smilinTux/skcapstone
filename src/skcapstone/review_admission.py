"""Shared, read-only governed-review admission diagnostics."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

LOGICAL_REVIEWER_SEATS = frozenset({"link", "mero", "seraph"})


def qualified_reviewer_seats(core: Mapping[str, object] | None = None) -> frozenset[str]:
    """Return default and configured logical reviewer seats, failing closed."""
    meta = core.get("meta") if isinstance(core, Mapping) else {}
    raw = meta.get("qualified_reviewer_seats", []) if isinstance(meta, dict) else []
    if not isinstance(raw, list):
        return LOGICAL_REVIEWER_SEATS
    configured = {
        str(value).strip().casefold()
        for value in raw
        if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9-]*", value.strip())
    }
    return LOGICAL_REVIEWER_SEATS | configured


def governed_review_seat(
    labels: Sequence[str], qualified_seats: frozenset[str] = LOGICAL_REVIEWER_SEATS
) -> str | None:
    """Return one supported logical review seat, or fail closed with ``None``."""
    seats = {
        str(label).strip().lower().removeprefix("seat-")
        for label in labels
        if str(label).strip().lower().startswith("seat-")
    }
    admitted = seats & qualified_seats
    return next(iter(admitted)) if len(seats) == len(admitted) == 1 else None


def reviewer_capacity(
    home: Path,
    core: Mapping[str, object],
    labels: Sequence[str],
    producer: str,
    reviewer: str,
) -> tuple[int, int]:
    """Return occupied and total qualified SKGateway slots for this card."""
    from .fleet.review_capacity import (
        aggregate_review_capacity,
        eligible_review_routes,
        load_route_occupancy,
    )

    match = re.search(r"\[(S|M|L|XL)\]", str(core.get("title") or ""))
    path = home / "evidence" / "fleet-review-routes.json"
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0, 0
    occupancy, ambiguous = load_route_occupancy(home)
    if match is None or ambiguous:
        return 0, 0
    routes = eligible_review_routes(
        snapshot,
        match.group(1),
        labels,
        producer,
        reviewer,
        occupancy,
        declared_seat=governed_review_seat(labels, qualified_reviewer_seats(core)),
        qualified_seats=qualified_reviewer_seats(core),
    )
    total = aggregate_review_capacity(routes, sum(int(route["free"]) for route in routes))
    return sum(occupancy.values()), total


def reviewer_candidate_reasons(
    identity: str,
    *,
    producer: str,
    declared_seat: str | None = None,
    qualified_seats: set[str] | frozenset[str] = LOGICAL_REVIEWER_SEATS,
    available: bool = True,
    capacity_available: bool = True,
    expected_claim_revision: str | None = None,
    current_claim_revision: str | None = None,
) -> tuple[str, ...]:
    """Evaluate one reviewer with the same host-neutral facts used by placement."""
    normalized = identity.strip().casefold().replace("_", "-")
    seat = str(declared_seat or "").strip().casefold()
    if not seat:
        matches = {
            candidate
            for candidate in qualified_seats
            if normalized == candidate
            or normalized.startswith(candidate + "-")
            or ("-" + candidate + "-") in ("-" + normalized + "-")
        }
        seat = next(iter(matches)) if len(matches) == 1 else ""
    reasons: list[str] = []
    if not seat or seat not in qualified_seats:
        reasons.append("unqualified-reviewer")
    producer_normalized = producer.strip().casefold().replace("_", "-")
    producer_seat = {
        candidate
        for candidate in qualified_seats
        if producer_normalized == candidate
        or producer_normalized.startswith(candidate + "-")
        or ("-" + candidate + "-") in ("-" + producer_normalized + "-")
    }
    if normalized == producer_normalized or (seat and producer_seat == {seat}):
        reasons.append("producer-self-review")
    if not available:
        reasons.append("reviewer-unavailable")
    if not capacity_available:
        reasons.append("capacity")
    if expected_claim_revision != current_claim_revision and (
        expected_claim_revision is not None or current_claim_revision is not None
    ):
        reasons.append("stale-generation")
    return tuple(reasons)


def governed_review_metadata(
    core: Mapping[str, object], labels: Sequence[str]
) -> tuple[str, str] | None:
    """Return typed producer evidence for an explicitly labelled review."""
    normalized = {str(label).strip().lower() for label in labels}
    if "review" not in normalized:
        return None
    links = core.get("links") if isinstance(core.get("links"), dict) else {}
    meta = core.get("meta") if isinstance(core.get("meta"), dict) else {}
    producer = str(links.get("producer_identity") or meta.get("producer_identity") or "").strip()
    evidence = (
        str(links.get("candidate_evidence_sha256") or meta.get("candidate_evidence_sha256") or "")
        .strip()
        .lower()
    )
    if producer or evidence:
        return (
            (producer, evidence) if producer and re.fullmatch(r"[0-9a-f]{64}", evidence) else None
        )
    description = str(core.get("description") or "")
    producer_match = re.search(r"Producer identity:\s*([^.]*)\.", description)
    evidence_match = re.search(r"sha256=([0-9a-f]{64})(?:\.|\s|$)", description)
    if not producer_match or not producer_match.group(1).strip() or not evidence_match:
        return None
    return producer_match.group(1).strip(), evidence_match.group(1)


def governed_review_source_binding(
    core: Mapping[str, object], labels: Sequence[str]
) -> tuple[str, str] | None:
    """Return the immutable source card and revision for Seraph review work."""
    if "review" not in {str(label).strip().lower() for label in labels}:
        return None
    links = core.get("links") if isinstance(core.get("links"), dict) else {}
    meta = core.get("meta") if isinstance(core.get("meta"), dict) else {}
    source = str(links.get("link_source_card") or meta.get("link_source_card") or "").strip()
    head = str(links.get("link_head_revision") or meta.get("link_head_revision") or "").strip()
    if not source or not re.fullmatch(r"[0-9a-f]{40}", head):
        return None
    return source, head


def governed_review_gate_reasons(
    core: Mapping[str, object],
    labels: Sequence[str],
    *,
    dependency_blocked: bool = False,
    owned: bool = False,
    capacity_available: bool = True,
) -> tuple[str, ...]:
    """Return stable reason codes used by CLI and POOL_V2 diagnostics."""
    normalized = {str(label).strip().lower() for label in labels}
    if "review" not in normalized:
        return ()
    reasons = []
    if governed_review_seat(tuple(normalized), qualified_reviewer_seats(core)) is None:
        reasons.append("wrong-seat")
    if governed_review_metadata(core, labels) is None:
        reasons.append("absent-typed-metadata")
    if governed_review_source_binding(core, labels) is None:
        reasons.append("absent-source-binding")
    if dependency_blocked:
        reasons.append("dependency")
    if owned:
        reasons.append("ownership")
    if not capacity_available:
        reasons.append("capacity")
    return tuple(reasons)


def assert_governed_review_claim(home: Path, card_id: str, agent: str) -> None:
    """Fail closed when a SKCapstone claim bypasses governed review admission."""
    from .card_store import CardStore

    card = CardStore(home).fold(card_id)
    if card is None:
        return
    labels = [str(label).strip().lower() for label in card.labels]
    if "review" not in labels:
        return
    core = {
        "title": card.title,
        "description": card.description,
        "links": card.links,
        "meta": card.meta,
    }
    reasons = governed_review_gate_reasons(core, labels)
    reviewer = agent.strip().lower()
    elastic = re.fullmatch(
        rf"pi-codex-review-[a-z0-9][a-z0-9-]*-{re.escape(card_id.lower())}", reviewer
    )
    metadata = governed_review_metadata(core, labels)
    producer = metadata[0] if metadata else ""
    busy, target = reviewer_capacity(home, core, labels, producer, reviewer)
    candidate_reasons = reviewer_candidate_reasons(
        reviewer,
        producer=producer,
        declared_seat=(
            governed_review_seat(labels, qualified_reviewer_seats(core)) if elastic else None
        ),
        qualified_seats=qualified_reviewer_seats(core),
        capacity_available=target > 0,
    )
    if "unqualified-reviewer" in candidate_reasons:
        reasons = ("wrong-reviewer", *reasons)
    reasons = (
        *(reason for reason in candidate_reasons if reason != "unqualified-reviewer"),
        *reasons,
    )
    if reasons:
        raise ValueError("governed review claim denied: " + ", ".join(dict.fromkeys(reasons)))
