"""Bounded, restart-safe metadata collection for SKRSI."""

from __future__ import annotations

import hashlib
import json
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .skrsi_registry import (
    FORBIDDEN_KEYS,
    AppendOnlyOutbox,
    SKRSIError,
    canonical_json,
    make_record,
    redact_metadata,
)

_ALLOWED = frozenset(
    {
        "source",
        "source_revision",
        "cursor",
        "event_id",
        "event_type",
        "occurred_at",
        "recorded_at",
        "natural_key",
        "target_ref",
        "body_hash",
        "metadata",
        "quality",
        "route",
        "evidence_ref",
        "cleanup_ref",
        "recovery_ref",
        "status",
        "sample_count",
    }
)
_SECRET_PARTS = ("secret", "token", "password", "credential", "private_key")


@dataclass(frozen=True)
class AdapterContract:
    """One mediated source and its downstream recovery owner."""

    source: str
    authority: str
    handoff_owner: str
    timeout_seconds: float = 2.0
    retry_attempts: int = 2

    def __post_init__(self) -> None:
        if not self.source or not self.authority or not self.handoff_owner:
            raise ValueError("source, authority, and handoff owner are required")
        if self.timeout_seconds <= 0 or self.retry_attempts < 0:
            raise ValueError("invalid adapter bounds")


@dataclass(frozen=True)
class Measurement:
    name: str
    value: float
    unit: str
    target_revision: str
    sample_count: int
    freshness_seconds: float | None
    missing: bool = False
    quality: str = "complete"
    dimensions: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CollectionResult:
    accepted: int
    rejected: int
    duplicates: int
    overloaded: bool
    cursor: str
    measurements: tuple[Measurement, ...]


def _utc(value: object, *, name: str) -> datetime:
    if not isinstance(value, str):
        raise SKRSIError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SKRSIError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise SKRSIError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _reject_protected(value: object, path: str = "metadata") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise SKRSIError("metadata keys must be strings")
            lowered = key.lower()
            if lowered in FORBIDDEN_KEYS or any(part in lowered for part in _SECRET_PARTS):
                raise SKRSIError(f"protected field at {path}.{key}")
            _reject_protected(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_protected(item, f"{path}[{index}]")


class BoundedCollector:
    """Collect one canonical observation per natural key without silent loss."""

    def __init__(
        self,
        outbox: AppendOnlyOutbox,
        *,
        source: str,
        target_ref: str,
        authority: str | None = None,
        handoff_owner: str = "skrsi",
        target_revision: str = "1",
        queue_size: int = 10_000,
        timeout_seconds: float = 2.0,
        retry_attempts: int = 2,
        max_age: timedelta = timedelta(days=395),
        max_future_skew: timedelta = timedelta(minutes=5),
    ) -> None:
        if queue_size < 1 or max_age <= timedelta(0) or max_future_skew < timedelta(0):
            raise ValueError("invalid collector bounds")
        self.contract = AdapterContract(
            source, authority or source, handoff_owner, timeout_seconds, retry_attempts
        )
        self.outbox = outbox
        self.target_ref = target_ref
        self.target_revision = target_revision
        self.max_age = max_age
        self.max_future_skew = max_future_skew
        self._queue: queue.Queue[Mapping[str, Any]] = queue.Queue(maxsize=queue_size)
        self._seen, self._cursor, self._last_occurred_at = self._load_state()
        self._lock = threading.Lock()
        self._metrics = {
            "accepted": 0,
            "rejected": 0,
            "malformed": 0,
            "overload": 0,
            "duplicates": 0,
        }

    @property
    def cursor(self) -> str:
        return self._cursor

    @property
    def capacity(self) -> int:
        return self._queue.maxsize

    def _load_state(self) -> tuple[dict[str, str], str, datetime | None]:
        seen: dict[str, str] = {}
        cursor = ""
        last_occurred_at: datetime | None = None
        for entry in self.outbox.entries():
            record = json.loads(entry.serialized)
            payload = record.get("payload", {})
            if (
                record.get("actor") == self.contract.source
                and record.get("event_type") != "skrsi.collection_error"
                and isinstance(payload.get("natural_key"), str)
                and isinstance(payload.get("envelope_hash"), str)
            ):
                seen[payload["natural_key"]] = payload["envelope_hash"]
                record_cursor = payload.get("cursor")
                if isinstance(record_cursor, str) and record_cursor >= cursor:
                    cursor = record_cursor
                occurred = _utc(record.get("occurred_at"), name="occurred_at")
                if last_occurred_at is None or occurred > last_occurred_at:
                    last_occurred_at = occurred
        return seen, cursor, last_occurred_at

    def submit(self, envelope: Mapping[str, Any]) -> bool:
        if not isinstance(envelope, Mapping):
            with self._lock:
                self._metrics["malformed"] += 1
            return False
        try:
            self._queue.put_nowait(dict(envelope))
            return True
        except queue.Full:
            with self._lock:
                self._metrics["overload"] += 1
            return False

    def _validate(
        self, item: Mapping[str, Any], *, now: datetime
    ) -> tuple[str, str, dict[str, Any], datetime, datetime]:
        natural = item.get("natural_key")
        if set(item) - _ALLOWED or not isinstance(natural, str) or not natural:
            raise SKRSIError("malformed envelope")
        if item.get("source") != self.contract.source:
            raise SKRSIError("unauthorized source")
        _reject_protected(item)
        cursor = item.get("cursor")
        if not isinstance(cursor, str) or not cursor:
            raise SKRSIError("cursor is required")
        if self._cursor and cursor < self._cursor:
            raise SKRSIError("stale cursor")
        occurred = _utc(item.get("occurred_at"), name="occurred_at")
        recorded = _utc(item.get("recorded_at", item.get("occurred_at")), name="recorded_at")
        if occurred > now + self.max_future_skew or recorded > now + self.max_future_skew:
            raise SKRSIError("future timestamp")
        if occurred < now - self.max_age:
            raise SKRSIError("stale timestamp")
        if recorded < occurred:
            raise SKRSIError("recorded_at precedes occurred_at")
        body_hash = item.get("body_hash")
        if body_hash is not None and (
            not isinstance(body_hash, str)
            or len(body_hash) != 64
            or any(character not in "0123456789abcdef" for character in body_hash.lower())
        ):
            raise SKRSIError("invalid body hash")
        envelope_hash = hashlib.sha256(canonical_json(item)).hexdigest()
        payload = {
            "natural_key": natural,
            "source": self.contract.source,
            "source_authority": self.contract.authority,
            "handoff_owner": self.contract.handoff_owner,
            "cursor": cursor,
            "envelope_hash": envelope_hash,
            "value": 1,
            "unit": "metadata_event",
            "cohort": "estate",
            "sample_id": natural,
            "collection_quality": item.get("quality", "complete"),
            "metadata": redact_metadata(item.get("metadata", {})),
        }
        for key in (
            "body_hash",
            "route",
            "evidence_ref",
            "cleanup_ref",
            "recovery_ref",
            "status",
        ):
            if key in item:
                payload[key] = item[key]
        return natural, cursor, payload, occurred, recorded

    def _record_error(self, item: Mapping[str, Any], reason: str, *, now: datetime) -> None:
        try:
            source = canonical_json(item)
        except (TypeError, ValueError):
            source = canonical_json(
                {
                    "keys": sorted(str(key) for key in item),
                    "types": sorted(type(value).__name__ for value in item.values()),
                }
            )
        fingerprint = hashlib.sha256(source).hexdigest()
        record = make_record(
            "Observation",
            actor=self.contract.source,
            target_ref=self.target_ref,
            event_id=f"collection-error-{fingerprint[:24]}",
            event_type="skrsi.collection_error",
            occurred_at=now,
            recorded_at=now,
            payload={
                "natural_key": f"error:{fingerprint}",
                "value": 1,
                "unit": "metadata_event",
                "source": self.contract.source,
                "cohort": "estate",
                "sample_id": fingerprint,
                "collection_quality": "malformed",
                "reason": reason[:120],
                "envelope_hash": fingerprint,
                "handoff_owner": self.contract.handoff_owner,
            },
        )
        if not any(
            entry.idempotency_key == record.idempotency_key for entry in self.outbox.entries()
        ):
            self.outbox.append(record)

    def drain(self, *, limit: int | None = None, now: datetime | None = None) -> CollectionResult:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("now must include a timezone")
        now = now.astimezone(timezone.utc)
        accepted = rejected = duplicates = 0
        count = self._queue.qsize() if limit is None else min(limit, self._queue.qsize())
        for _ in range(count):
            item = self._queue.get_nowait()
            try:
                natural, cursor, payload, occurred, recorded = self._validate(item, now=now)
                envelope_hash = payload["envelope_hash"]
                with self._lock:
                    prior = self._seen.get(natural)
                    if prior:
                        if prior != envelope_hash:
                            raise SKRSIError("natural key conflict")
                        self._metrics["duplicates"] += 1
                        duplicates += 1
                        continue
                    record = make_record(
                        "Observation",
                        actor=self.contract.source,
                        target_ref=self.target_ref,
                        payload=payload,
                        event_type=str(item.get("event_type", "skrsi.observation")),
                        event_id=str(item.get("event_id", natural)),
                        occurred_at=occurred,
                        recorded_at=recorded,
                    )
                    self.outbox.append(record)
                    self._seen[natural] = envelope_hash
                    self._cursor = cursor
                    self._last_occurred_at = occurred
                    self._metrics["accepted"] += 1
                    accepted += 1
            except (SKRSIError, TypeError, ValueError) as exc:
                self._record_error(item, str(exc), now=now)
                with self._lock:
                    self._metrics["rejected"] += 1
                    self._metrics["malformed"] += 1
                rejected += 1
        measurements = self._measurements(now, accepted, rejected, duplicates)
        return CollectionResult(
            accepted,
            rejected,
            duplicates,
            bool(self._metrics["overload"]),
            self._cursor,
            measurements,
        )

    def _measurements(
        self, now: datetime, accepted: int, rejected: int, duplicates: int
    ) -> tuple[Measurement, ...]:
        freshness = (
            None
            if self._last_occurred_at is None
            else max(0.0, (now - self._last_occurred_at).total_seconds())
        )
        dimensions = {
            "source": self.contract.source,
            "authority": self.contract.authority,
            "handoff_owner": self.contract.handoff_owner,
        }
        values = (
            ("skrsi.collector.events.accepted", accepted, "{event}", "complete"),
            ("skrsi.collector.events.rejected", rejected, "{event}", "malformed"),
            ("skrsi.collector.events.duplicate", duplicates, "{event}", "complete"),
            ("skrsi.collector.events.overload", self._metrics["overload"], "{event}", "degraded"),
            ("skrsi.collector.queue.depth", self._queue.qsize(), "{event}", "complete"),
            ("skrsi.collector.cursor.present", int(bool(self._cursor)), "1", "complete"),
        )
        return tuple(
            Measurement(
                name,
                float(value),
                unit,
                self.target_revision,
                accepted + rejected + duplicates,
                freshness,
                missing=freshness is None,
                quality="missing" if freshness is None else quality,
                dimensions=dimensions,
            )
            for name, value, unit, quality in values
        )

    def metric_snapshot(self, *, now: datetime | None = None) -> tuple[Measurement, ...]:
        return self.drain(limit=0, now=now).measurements
