"""Read-only Link oversight projection.

The projector accepts already-observed scheduler, CardStore, review, and gateway
records. It returns evidence and recommendations only. It has no fleet, card,
review, trunk, or deployment actuator.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

Lane = Literal["codex", "glm", "qwen"]
ProcessState = Literal["live", "exited"]


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include timezone: {value}")
    return parsed.astimezone(timezone.utc)


def _check_provenance(value: str) -> None:
    if not value.strip():
        raise ValueError("provenance must not be empty")


@dataclass(frozen=True)
class LaneConfig:
    lane: Lane
    configured_slots: int
    provenance: str

    def __post_init__(self) -> None:
        if self.configured_slots < 0:
            raise ValueError("configured_slots must be non-negative")
        _check_provenance(self.provenance)


@dataclass(frozen=True)
class ProcessObservation:
    lane: Lane
    identity: str
    state: ProcessState
    observed_at: str
    provenance: str
    claim_revision: str | None = None

    def __post_init__(self) -> None:
        _time(self.observed_at)
        _check_provenance(self.provenance)


@dataclass(frozen=True)
class ChurnEvent:
    lane: Lane
    kind: Literal["launch", "exit"]
    occurred_at: str
    identity: str
    provenance: str

    def __post_init__(self) -> None:
        _time(self.occurred_at)
        _check_provenance(self.provenance)


@dataclass(frozen=True)
class ClaimObservation:
    card_id: str
    owner: str
    claim_revision: str
    claimed_at: str
    provenance: str

    def __post_init__(self) -> None:
        _time(self.claimed_at)
        _check_provenance(self.provenance)


@dataclass(frozen=True)
class ReviewAssignment:
    card_id: str
    author_identity: str
    author_host: str
    author_session: str
    author_workspace: str
    reviewer_identity: str
    reviewer_host: str
    reviewer_session: str
    reviewer_workspace: str
    provenance: str

    def __post_init__(self) -> None:
        _check_provenance(self.provenance)


@dataclass(frozen=True)
class GatewayObservation:
    lane: Lane
    latency_ms: float
    observed_at: str
    provenance: str
    terminal_error: str | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.latency_ms) or self.latency_ms < 0:
            raise ValueError("latency_ms must be finite and non-negative")
        _time(self.observed_at)
        _check_provenance(self.provenance)


@dataclass(frozen=True)
class AgeObservation:
    subject: str
    age_seconds: float
    threshold_seconds: float
    observed_at: str
    provenance: str

    def __post_init__(self) -> None:
        if min(self.age_seconds, self.threshold_seconds) < 0:
            raise ValueError("ages must be non-negative")
        _time(self.observed_at)
        _check_provenance(self.provenance)


@dataclass(frozen=True)
class OversightInput:
    observed_at: str
    window_start: str
    stale_claim_seconds: float
    lanes: tuple[LaneConfig, ...]
    processes: tuple[ProcessObservation, ...] = ()
    churn: tuple[ChurnEvent, ...] = ()
    claims: tuple[ClaimObservation, ...] = ()
    reviews: tuple[ReviewAssignment, ...] = ()
    gateway: tuple[GatewayObservation, ...] = ()
    ages: tuple[AgeObservation, ...] = ()
    max_churn_events: int = 100

    def __post_init__(self) -> None:
        if _time(self.window_start) > _time(self.observed_at):
            raise ValueError("window_start must not follow observed_at")
        if self.stale_claim_seconds < 0:
            raise ValueError("stale_claim_seconds must be non-negative")
        if self.max_churn_events < 1:
            raise ValueError("max_churn_events must be positive")
        lane_names = [lane.lane for lane in self.lanes]
        if len(lane_names) != len(set(lane_names)):
            raise ValueError("lane configuration must be unique")


@dataclass(frozen=True)
class OversightSnapshot:
    schema: Literal["link-oversight/v1"]
    observed_at: str
    window_start: str
    lanes: tuple[dict[str, Any], ...]
    churn: dict[str, Any]
    stale_claim_process_joins: tuple[dict[str, Any], ...]
    duplicate_review_dimensions: tuple[dict[str, Any], ...]
    gateway: tuple[dict[str, Any], ...]
    age_threshold_breaches: tuple[dict[str, Any], ...]
    recommendations: tuple[dict[str, str], ...]
    provenance: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    result = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(result, 3)


def project(source: OversightInput) -> OversightSnapshot:
    """Build one deterministic snapshot without mutating any source."""
    observed = _time(source.observed_at)
    window_start = _time(source.window_start)
    lane_names = sorted(lane.lane for lane in source.lanes)
    configured = {lane.lane: lane for lane in source.lanes}
    live = [process for process in source.processes if process.state == "live"]

    lanes = tuple(
        {
            "lane": lane,
            "configured_slots": configured[lane].configured_slots,
            "live_slots": sum(process.lane == lane for process in live),
            "free_slots": max(
                0,
                configured[lane].configured_slots - sum(process.lane == lane for process in live),
            ),
            "provenance": tuple(
                sorted(
                    {configured[lane].provenance}
                    | {process.provenance for process in live if process.lane == lane}
                )
            ),
        }
        for lane in lane_names
    )

    bounded_churn = sorted(
        (event for event in source.churn if window_start <= _time(event.occurred_at) <= observed),
        key=lambda event: (event.occurred_at, event.lane, event.kind, event.identity),
    )[-source.max_churn_events :]
    churn = {
        "limit": source.max_churn_events,
        "truncated": len(
            [
                event
                for event in source.churn
                if window_start <= _time(event.occurred_at) <= observed
            ]
        )
        > source.max_churn_events,
        "launches": sum(event.kind == "launch" for event in bounded_churn),
        "exits": sum(event.kind == "exit" for event in bounded_churn),
        "events": tuple(asdict(event) for event in bounded_churn),
    }

    live_joins = {(process.identity, process.claim_revision) for process in live}
    stale = tuple(
        {
            "card_id": claim.card_id,
            "owner": claim.owner,
            "claim_revision": claim.claim_revision,
            "age_seconds": round((observed - _time(claim.claimed_at)).total_seconds(), 3),
            "provenance": claim.provenance,
        }
        for claim in sorted(source.claims, key=lambda item: (item.card_id, item.claim_revision))
        if (claim.owner, claim.claim_revision) not in live_joins
        and (observed - _time(claim.claimed_at)).total_seconds() >= source.stale_claim_seconds
    )

    duplicate_reviews = []
    for review in sorted(source.reviews, key=lambda item: item.card_id):
        dimensions = tuple(
            dimension
            for dimension in ("identity", "host", "session", "workspace")
            if getattr(review, f"author_{dimension}") == getattr(review, f"reviewer_{dimension}")
        )
        if dimensions:
            duplicate_reviews.append(
                {
                    "card_id": review.card_id,
                    "dimensions": dimensions,
                    "provenance": review.provenance,
                }
            )

    gateway = []
    for lane in lane_names:
        observations = sorted(
            (
                item
                for item in source.gateway
                if item.lane == lane and window_start <= _time(item.observed_at) <= observed
            ),
            key=lambda item: (item.observed_at, item.provenance),
        )
        gateway.append(
            {
                "lane": lane,
                "sample_count": len(observations),
                "p50_latency_ms": _percentile([item.latency_ms for item in observations], 0.50),
                "p95_latency_ms": _percentile([item.latency_ms for item in observations], 0.95),
                "terminal_errors": tuple(
                    {
                        "error": item.terminal_error,
                        "observed_at": item.observed_at,
                        "provenance": item.provenance,
                    }
                    for item in observations
                    if item.terminal_error is not None
                ),
                "provenance": tuple(sorted({item.provenance for item in observations})),
            }
        )

    age_breaches = tuple(
        asdict(item)
        for item in sorted(source.ages, key=lambda item: (item.subject, item.observed_at))
        if item.age_seconds >= item.threshold_seconds
    )
    recommendations = []
    if stale:
        recommendations.append(
            {"kind": "inspect_stale_claim_joins", "reason": f"count={len(stale)}"}
        )
    if duplicate_reviews:
        recommendations.append(
            {"kind": "assign_distinct_reviewer", "reason": f"count={len(duplicate_reviews)}"}
        )
    if any(item["terminal_errors"] for item in gateway):
        recommendations.append(
            {"kind": "inspect_gateway_errors", "reason": "terminal errors observed"}
        )
    if age_breaches:
        recommendations.append(
            {"kind": "inspect_age_breaches", "reason": f"count={len(age_breaches)}"}
        )

    provenance = tuple(
        sorted(
            {
                item.provenance
                for collection in (
                    source.lanes,
                    source.processes,
                    source.churn,
                    source.claims,
                    source.reviews,
                    source.gateway,
                    source.ages,
                )
                for item in collection
            }
        )
    )
    return OversightSnapshot(
        schema="link-oversight/v1",
        observed_at=source.observed_at,
        window_start=source.window_start,
        lanes=lanes,
        churn=churn,
        stale_claim_process_joins=stale,
        duplicate_review_dimensions=tuple(duplicate_reviews),
        gateway=tuple(gateway),
        age_threshold_breaches=age_breaches,
        recommendations=tuple(recommendations),
        provenance=provenance,
    )


def append_evidence(path: Path, snapshot: OversightSnapshot) -> str:
    """Serializer-build, validate, then append one JSONL evidence record.

    Returns the SHA-256 of the exact appended bytes. O_APPEND prevents this
    evidence-only API from replacing existing records.
    """
    line = json.dumps(snapshot.to_dict(), sort_keys=True, separators=(",", ":"))
    parsed = json.loads(line)
    if parsed.get("schema") != "link-oversight/v1":
        raise ValueError("invalid oversight snapshot schema")
    encoded = (line + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, encoded)
    finally:
        os.close(descriptor)
    return hashlib.sha256(encoded).hexdigest()
