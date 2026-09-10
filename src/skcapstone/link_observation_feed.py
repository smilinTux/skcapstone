"""Read-only, hash-bound observation input for the Link seat.

The producer of this feed may use a mediated GitHub client elsewhere. Link
only reads the resulting typed observations. The feed contains no credentials
and cannot authorize a merge, deployment, or external action.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .link_cycle import (
    ProducerIdentity,
    PullRequestObservation,
    ReviewerIdentity,
    _digest,
)

SCHEMA = "skfleet.link-observation-feed/v1"
REVIEWER_SCHEMA = "skfleet.reviewer-identity/v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_MAX_AGE = timedelta(minutes=15)


class ObservationFeedError(ValueError):
    """The feed cannot safely be consumed by Link."""


@dataclass(frozen=True)
class LinkObservationRecord:
    observation: PullRequestObservation
    review_card_id: str | None
    review_card_revision: str | None


@dataclass(frozen=True)
class LinkObservationFeed:
    source_revision: str
    observed_at: str
    evidence_sha256: str
    producer: ProducerIdentity
    records: tuple[LinkObservationRecord, ...]
    reviewer_candidates: tuple[ReviewerIdentity, ...]


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ObservationFeedError("feed timestamp is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ObservationFeedError("feed timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _as_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ObservationFeedError(f"{label} must be an object")
    return value


def _tuple_strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ObservationFeedError(f"{label} must be a string array")
    return tuple(value)


def _observation(value: object) -> PullRequestObservation:
    row = _as_mapping(value, "observation")
    try:
        observation = PullRequestObservation(
            repository=str(row["repository"]),
            number=int(row["number"]),
            title=str(row["title"]),
            author=str(row["author"]),
            head_sha=str(row["head_sha"]),
            base_sha=str(row["base_sha"]),
            ci_state=str(row["ci_state"]),
            conflict_state=str(row["conflict_state"]),
            review_requests=_tuple_strings(row.get("review_requests", []), "review_requests"),
            source_card=(str(row["source_card"]) if row.get("source_card") is not None else None),
            card_generation=(
                str(row["card_generation"]) if row.get("card_generation") is not None else None
            ),
            observed_at=str(row["observed_at"]),
            active_review_card=(
                str(row["active_review_card"])
                if row.get("active_review_card") is not None
                else None
            ),
            categories=_tuple_strings(row.get("categories", ["source"]), "categories"),
            lineage_outcomes=_tuple_strings(row.get("lineage_outcomes", []), "lineage_outcomes"),
        )
        observation.validate()
        return observation
    except (KeyError, TypeError, ValueError) as exc:
        raise ObservationFeedError(f"malformed PR observation: {exc}") from exc


def _reviewer(value: object) -> ReviewerIdentity:
    row = _as_mapping(value, "reviewer")
    if row.get("schema") != REVIEWER_SCHEMA:
        raise ObservationFeedError("reviewer identity schema is invalid")
    if row.get("seat") != "seraph":
        raise ObservationFeedError("reviewer identity seat is not eligible")
    required = ("name", "identity", "host", "session", "workspace")
    if any(not isinstance(row.get(field), str) or not row[field].strip() for field in required):
        raise ObservationFeedError("reviewer identity fields are incomplete")
    if row["name"].strip().lower() != "seraph" or row["identity"].strip().lower().startswith(
        "jarvis"
    ):
        raise ObservationFeedError("reviewer identity name is not eligible")
    fingerprint = row.get("fingerprint")
    if not isinstance(fingerprint, str) or not _SHA256.fullmatch(fingerprint):
        raise ObservationFeedError("reviewer identity fingerprint is invalid")
    eligible = row.get("eligible", True)
    if eligible is not True:
        raise ObservationFeedError("reviewer identity eligibility is invalid")
    try:
        reviewer = ReviewerIdentity(
            name=row["name"],
            identity=row["identity"],
            host=row["host"],
            session=row["session"],
            workspace=row["workspace"],
            eligible=eligible,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ObservationFeedError(f"malformed reviewer identity: {exc}") from exc
    if not reviewer.name.strip() or not all(reviewer.distinct_key()):
        raise ObservationFeedError("reviewer identity is incomplete")
    return reviewer


def _producer(value: object) -> ProducerIdentity:
    row = _as_mapping(value, "producer")
    try:
        producer = ProducerIdentity(
            identity=str(row["identity"]),
            host=str(row["host"]),
            session=str(row["session"]),
            workspace=str(row["workspace"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ObservationFeedError(f"malformed producer identity: {exc}") from exc
    if not all(producer.distinct_key()):
        raise ObservationFeedError("producer identity is incomplete")
    return producer


def _canonical_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        "source_revision": raw.get("source_revision"),
        "observed_at": raw.get("observed_at"),
        "producer": raw.get("producer"),
        "records": raw.get("records"),
        "reviewer_candidates": raw.get("reviewer_candidates"),
    }
    if "lineage_status" in raw:
        payload["lineage_status"] = raw.get("lineage_status")
    return payload


def load_observation_feed(
    path: Path,
    *,
    now: datetime | None = None,
    max_age: timedelta = DEFAULT_MAX_AGE,
) -> LinkObservationFeed:
    """Load one fresh, self-hashed feed or raise a bounded input error."""

    if not path.exists():
        raise ObservationFeedError("observation_feed_missing")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ObservationFeedError("observation_feed_unreadable") from exc
    data = _as_mapping(raw, "observation feed")
    if data.get("schema") != SCHEMA:
        raise ObservationFeedError("observation_feed_schema_mismatch")
    source_revision = str(data.get("source_revision") or "")
    observed_at = str(data.get("observed_at") or "")
    supplied_hash = str(data.get("evidence_sha256") or "")
    if not source_revision or not observed_at or len(supplied_hash) != 64:
        raise ObservationFeedError("observation_feed_identity_incomplete")
    try:
        expected_hash = _digest(_canonical_payload(data))
    except (TypeError, ValueError) as exc:
        raise ObservationFeedError("observation_feed_payload_malformed") from exc
    if supplied_hash != expected_hash:
        raise ObservationFeedError("observation_feed_evidence_mismatch")
    timestamp = _parse_time(observed_at)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if timestamp > current + timedelta(seconds=30):
        raise ObservationFeedError("observation_feed_clock_ahead")
    if current - timestamp > max_age:
        raise ObservationFeedError("observation_feed_stale")
    raw_records = data.get("records")
    raw_reviewers = data.get("reviewer_candidates")
    if not isinstance(raw_records, list) or not isinstance(raw_reviewers, list):
        raise ObservationFeedError("observation_feed_records_missing")
    records: list[LinkObservationRecord] = []
    for value in raw_records:
        row = _as_mapping(value, "record")
        observation = _observation(row.get("observation"))
        observation_time = _parse_time(observation.observed_at)
        if observation_time > current + timedelta(seconds=30):
            raise ObservationFeedError("observation_clock_ahead")
        if current - observation_time > max_age:
            raise ObservationFeedError("observation_stale")
        if observation_time > timestamp + timedelta(seconds=30):
            raise ObservationFeedError("observation_after_feed_timestamp")
        review_card_id = row.get("review_card_id")
        review_card_revision = row.get("review_card_revision")
        if (review_card_id is None) != (review_card_revision is None):
            raise ObservationFeedError("review card identity is incomplete")
        records.append(
            LinkObservationRecord(
                observation=observation,
                review_card_id=str(review_card_id) if review_card_id is not None else None,
                review_card_revision=(
                    str(review_card_revision) if review_card_revision is not None else None
                ),
            )
        )
    reviewers = tuple(_reviewer(value) for value in raw_reviewers)
    producer = _producer(data.get("producer"))
    return LinkObservationFeed(
        source_revision=source_revision,
        observed_at=observed_at,
        evidence_sha256=supplied_hash,
        producer=producer,
        records=tuple(records),
        reviewer_candidates=reviewers,
    )
