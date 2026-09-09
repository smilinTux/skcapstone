"""Fail-closed SKRSI adapters for lifecycle, fleet, mail, and review handoffs.

The objects in this module consume authority-owned metadata snapshots. They never
mutate an authority store. Their only durable output is the caller supplied
append-only SKRSI outbox. Structural events and evidence events remain distinct,
so lifecycle state and links cannot manufacture a review verdict.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from .skrsi_handoffs import FIRST_WAVE_HANDOFFS, HandoffContract  # noqa: F401
from .skrsi_registry import AppendOnlyOutbox, SKRSIError, canonical_json, make_record


class AuthorityUnavailableError(SKRSIError):
    """Authority truth cannot safely be used."""


ESTATE_HANDOFFS = {
    name: FIRST_WAVE_HANDOFFS[name]
    for name in ("cardstore-to-skrsi", "fleet-to-skrsi", "mail-to-skrsi", "evidence-to-review")
}


@dataclass(frozen=True)
class AuthoritySnapshot:
    """A revision-fenced view supplied by an authority adapter."""

    revision: str
    observed_at: datetime
    available: bool = True

    def validate(self, *, now: datetime, max_age: timedelta) -> None:
        if not self.available:
            raise AuthorityUnavailableError("authority unavailable")
        if not self.revision or self.observed_at.tzinfo is None:
            raise AuthorityUnavailableError("authority snapshot malformed")
        observed = self.observed_at.astimezone(timezone.utc)
        now = now.astimezone(timezone.utc)
        if observed > now:
            raise AuthorityUnavailableError("authority snapshot is from the future")
        if now - observed > max_age:
            raise AuthorityUnavailableError("authority snapshot is stale")


@dataclass(frozen=True)
class FanoutBudget:
    per_seat: int
    per_host: int
    per_evaluator: int
    per_route: int
    queue: int

    def __post_init__(self) -> None:
        if min(self.per_seat, self.per_host, self.per_evaluator, self.per_route, self.queue) < 1:
            raise ValueError("fan-out budgets must be positive")


@dataclass(frozen=True)
class EligibleWork:
    natural_key: str
    seat: str
    host: str
    evaluator: str
    route: str
    authority_revision: str
    quality_ok: bool
    authorized: bool
    review_invariants_ok: bool
    # A source head is a second, immutable idempotency fence.  It prevents a
    # retry from launching the same work under a different natural key.
    source_head: str = ""


@dataclass(frozen=True)
class FanoutReceipt:
    natural_key: str
    seat: str
    host: str
    evaluator: str
    route: str
    authority_revision: str
    receipt_id: str
    source_head: str = ""


LIFECYCLE_SEATS = frozenset({"seraph", "link", "mero", "niobe", "tank", "atlas", "skcapstone", "skdashboard", "skworld"})
DEFAULT_CHILD_MODEL = "sk-codex-mid"
# Child lanes are narrower than their parent seat.  Jarvis is intentionally
# absent: it is an authority seat, never a recurring lifecycle scheduler.
SEAT_CHILD_ROUTES = {
    "seraph": frozenset({"review"}),
    "link": frozenset({"integration", "review"}),
    "mero": frozenset({"lifecycle-analysis", "review"}),
    "niobe": frozenset({"dispatch"}),
    "tank": frozenset({"verification"}),
    "atlas": frozenset({"governance"}),
    "skcapstone": frozenset({"lifecycle"}),
    "skdashboard": frozenset({"lifecycle"}),
    "skworld": frozenset({"lifecycle"}),
}


def validate_child_request(seat: str, route: str, *, model: str = DEFAULT_CHILD_MODEL) -> None:
    """Fail closed when a lifecycle seat requests an out-of-scope child."""
    # Generic evaluator lanes remain valid for existing adapter callers; the
    # named lifecycle seats are the ones subject to the explicit role fence.
    if seat in LIFECYCLE_SEATS and route not in SEAT_CHILD_ROUTES[seat]:
        raise SKRSIError("child route exceeds lifecycle seat scope")
    if model != DEFAULT_CHILD_MODEL:
        raise SKRSIError("child model must use the bounded default")


class BoundedFleetFanout:
    """Allocate eligible work once while enforcing every saturation budget."""

    def __init__(
        self,
        budget: FanoutBudget,
        *,
        max_snapshot_age: timedelta = timedelta(minutes=2),
    ):
        self.budget = budget
        self.max_snapshot_age = max_snapshot_age
        self._claims: dict[str, FanoutReceipt] = {}
        self._source_claims: dict[str, FanoutReceipt] = {}
        self._lock = threading.Lock()

    def allocate(
        self,
        work: Iterable[EligibleWork],
        snapshot: AuthoritySnapshot,
        *,
        now: datetime | None = None,
    ) -> tuple[FanoutReceipt, ...]:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise AuthorityUnavailableError("current time is malformed")
        snapshot.validate(now=now, max_age=self.max_snapshot_age)
        selected: list[FanoutReceipt] = []
        with self._lock:
            counts: dict[tuple[str, str], int] = {}
            for receipt in self._claims.values():
                for dimension, value in (
                    ("seat", receipt.seat),
                    ("host", receipt.host),
                    ("evaluator", receipt.evaluator),
                    ("route", receipt.route),
                ):
                    counts[(dimension, value)] = counts.get((dimension, value), 0) + 1
            for item in work:
                validate_child_request(item.seat, item.route)
                if len(self._claims) >= self.budget.queue:
                    break
                if not item.natural_key or item.authority_revision != snapshot.revision:
                    continue
                if not (item.quality_ok and item.authorized and item.review_invariants_ok):
                    continue
                prior = self._claims.get(item.natural_key)
                # Source heads are globally unique launch identities.  This is
                # deliberately checked before capacity so retries remain
                # harmless even while the fleet is saturated.
                source_prior = self._source_claims.get(item.source_head) if item.source_head else None
                if source_prior is not None:
                    # A source-head replay is acknowledged by suppression, not
                    # returned as a second launch receipt.
                    continue
                if prior is not None:
                    # Replay is harmless, but a different seat can never inherit the claim.
                    if prior.seat == item.seat and prior.authority_revision == snapshot.revision:
                        selected.append(prior)
                    continue
                limits = {
                    "seat": self.budget.per_seat,
                    "host": self.budget.per_host,
                    "evaluator": self.budget.per_evaluator,
                    "route": self.budget.per_route,
                }
                dimensions = {
                    "seat": item.seat,
                    "host": item.host,
                    "evaluator": item.evaluator,
                    "route": item.route,
                }
                if any(
                    counts.get((kind, value), 0) >= limits[kind]
                    for kind, value in dimensions.items()
                ):
                    continue
                identity = canonical_json(
                    {
                        "natural_key": item.natural_key,
                        "seat": item.seat,
                        "host": item.host,
                        "evaluator": item.evaluator,
                        "route": item.route,
                        "authority_revision": snapshot.revision,
                        "source_head": item.source_head,
                    }
                )
                receipt = FanoutReceipt(
                    item.natural_key,
                    item.seat,
                    item.host,
                    item.evaluator,
                    item.route,
                    snapshot.revision,
                    hashlib.sha256(identity).hexdigest(),
                    item.source_head,
                )
                self._claims[item.natural_key] = receipt
                if item.source_head:
                    self._source_claims[item.source_head] = receipt
                selected.append(receipt)
                for kind, value in dimensions.items():
                    counts[(kind, value)] = counts.get((kind, value), 0) + 1
        return tuple(selected)


@dataclass(frozen=True)
class ReviewLaunchReceipt:
    source_card: str
    review_card: str
    head_revision: str
    claim_id: str
    reviewer: str
    process_id: str
    receipt_id: str


def canonical_review_card(source_card: str, head_revision: str) -> str:
    """One stable review-card identity per source card and immutable head."""

    identity = canonical_json({"source_card": source_card, "head_revision": head_revision})
    return hashlib.sha256(b"seraph-review\0" + identity).hexdigest()[:8]


class ReviewHandoff:
    """Atomically fence one independent reviewer, process, and launch receipt."""

    def __init__(self) -> None:
        self._receipts: dict[tuple[str, str], ReviewLaunchReceipt] = {}
        self._lock = threading.Lock()

    def launch(
        self,
        *,
        source_card: str,
        head_revision: str,
        producer: str,
        reviewer: str,
        process_id: str,
        authority_revision: str,
    ) -> ReviewLaunchReceipt:
        values = (source_card, head_revision, producer, reviewer, process_id, authority_revision)
        malformed_revision = len(head_revision) != 40 or any(
            character not in "0123456789abcdef" for character in head_revision
        )
        if not all(values) or malformed_revision:
            raise SKRSIError("review handoff is malformed")
        if producer == reviewer:
            raise SKRSIError("reviewer must be independent from producer")
        review_key = (source_card, head_revision)
        review_card = canonical_review_card(*review_key)
        claim_id = hashlib.sha256(
            canonical_json(
                {
                    "review_card": review_card,
                    "head_revision": head_revision,
                    "reviewer": reviewer,
                    "authority_revision": authority_revision,
                }
            )
        ).hexdigest()
        receipt_id = hashlib.sha256(
            canonical_json({"claim_id": claim_id, "process_id": process_id})
        ).hexdigest()
        candidate = ReviewLaunchReceipt(
            source_card, review_card, head_revision, claim_id, reviewer, process_id, receipt_id
        )
        with self._lock:
            prior = self._receipts.get(review_key)
            if prior is None:
                self._receipts[review_key] = candidate
                return candidate
            if prior == candidate:
                return prior
            raise SKRSIError("review source already has an immutable launch")


# Metrics are emitted only from explicitly named facts. In particular, a lifecycle
# status or link is not evidence of a review verdict.
METRIC_EVENTS = {
    "handoff.completed": ("skrsi.handoff.latency", "ms"),
    "queue.dequeued": ("skrsi.queue.time", "ms"),
    "review.completed": ("skrsi.reviewer.latency", "ms"),
    "claim.conflict": ("skrsi.claim.conflicts", "{event}"),
    "blocker.repeated": ("skrsi.blockers.repeated", "{event}"),
    "work.completed": ("skrsi.throughput", "{event}"),
    "review.verdict": ("skrsi.review.first_pass_rate", "1"),
    "rework.requested": ("skrsi.rework.count", "{event}"),
    "cleanup.completed": ("skrsi.cleanup.yield", "By"),
    "recovery.completed": ("skrsi.recovery.success_rate", "1"),
    "route.selected": ("skrsi.route.attribution", "{event}"),
}


def append_metric_event(
    outbox: AppendOnlyOutbox,
    event: Mapping[str, Any],
    *,
    actor: str = "skrsi-estate-adapter",
) -> str:
    """Validate and append one explicit metric fact to the SKRSI outbox."""

    allowed = {
        "event_id",
        "event_type",
        "occurred_at",
        "recorded_at",
        "value",
        "source",
        "target_ref",
        "cohort",
        "revision",
        "producer",
        "consumer",
        "seat",
        "host",
        "model",
        "route",
        "evidence_ref",
        "quality",
    }
    unknown = set(event) - allowed
    if unknown:
        raise SKRSIError(f"unknown metric event fields: {sorted(unknown)}")
    event_type = str(event.get("event_type", ""))
    if event_type not in METRIC_EVENTS:
        raise SKRSIError("metric event type is not explicit evidence")
    required = ("event_id", "occurred_at", "recorded_at", "source", "target_ref", "revision")
    if not all(event.get(field) for field in required):
        raise SKRSIError("metric event is missing authority metadata")
    name, unit = METRIC_EVENTS[event_type]
    value = event.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise SKRSIError("metric value must be numeric")
    metadata = {
        key: event[key]
        for key in ("producer", "consumer", "seat", "host", "model", "route", "evidence_ref")
        if event.get(key) is not None
    }
    record = make_record(
        "Observation",
        actor=actor,
        target_ref=str(event["target_ref"]),
        event_id=str(event["event_id"]),
        event_type=event_type,
        occurred_at=str(event["occurred_at"]),
        recorded_at=str(event["recorded_at"]),
        payload={
            "value": value,
            "unit": unit,
            "source": str(event["source"]),
            "cohort": str(event.get("cohort", "estate")),
            "sample_id": str(event["event_id"]),
            "collection_quality": str(event.get("quality", "complete")),
            "metric": name,
            "target_revision": str(event["revision"]),
            "metadata": metadata,
        },
    )
    if not any(entry.idempotency_key == record.idempotency_key for entry in outbox.entries()):
        outbox.append(record)
    return record.idempotency_key
