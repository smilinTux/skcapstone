"""Bounded, mediated metadata collection for SKRSI.

The collector accepts envelopes from estate-native adapters, never payloads.  It
canonicalizes and validates each observation before handing it to the existing
append-only outbox.  Structural lifecycle events and evidence are separate
records and are never combined into a verdict.
"""
from __future__ import annotations

import hashlib
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from .skrsi_registry import AppendOnlyOutbox, SKRSIError, make_record, redact_metadata

_ALLOWED = frozenset({
    "source", "source_revision", "cursor", "event_id", "event_type", "occurred_at",
    "recorded_at", "natural_key", "target_ref", "body_hash", "metadata", "quality",
    "route", "evidence_ref", "cleanup_ref", "recovery_ref", "status", "sample_count",
})
_PROTECTED = frozenset({"body", "payload", "prompt", "content", "secret", "token", "password"})

@dataclass(frozen=True)
class Measurement:
    name: str
    value: float
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
    overloaded: bool
    cursor: str
    measurements: tuple[Measurement, ...]

class BoundedCollector:
    """Single canonical observation per natural key with bounded backpressure."""
    def __init__(self, outbox: AppendOnlyOutbox, *, source: str, target_ref: str,
                 target_revision: str = "1", queue_size: int = 1000,
                 timeout_seconds: float = 2.0, retry_attempts: int = 2) -> None:
        if queue_size < 1 or retry_attempts < 0 or timeout_seconds <= 0:
            raise ValueError("invalid collector bounds")
        self.outbox, self.source, self.target_ref = outbox, source, target_ref
        self.target_revision, self.timeout_seconds, self.retry_attempts = target_revision, timeout_seconds, retry_attempts
        self._queue: queue.Queue[Mapping[str, Any]] = queue.Queue(maxsize=queue_size)
        self._seen: dict[str, str] = {}
        self._cursor: str = ""
        self._lock = threading.Lock()
        self._metrics: dict[str, int] = {"accepted": 0, "rejected": 0, "malformed": 0, "overload": 0, "duplicates": 0}

    @property
    def cursor(self) -> str: return self._cursor
    @property
    def capacity(self) -> int: return self._queue.maxsize

    def submit(self, envelope: Mapping[str, Any]) -> bool:
        if not isinstance(envelope, Mapping):
            self._metrics["malformed"] += 1; return False
        try:
            self._queue.put_nowait(dict(envelope)); return True
        except queue.Full:
            self._metrics["overload"] += 1; return False

    def _validate(self, item: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        unknown = set(item) - _ALLOWED
        if unknown or not item.get("natural_key") or item.get("source") != self.source:
            raise SKRSIError("malformed or unauthorized envelope")
        if any(key.lower() in _PROTECTED or any(x in key.lower() for x in ("secret", "token", "password")) for key in item):
            raise SKRSIError("protected field in envelope")
        natural = str(item["natural_key"])
        cursor = str(item.get("cursor", ""))
        if cursor and self._cursor and cursor < self._cursor:
            raise SKRSIError("stale cursor")
        metadata = redact_metadata(item.get("metadata", {}))
        if "body_hash" in item and item["body_hash"] is not None:
            body_hash = str(item["body_hash"])
            if len(body_hash) != 64 or any(c not in "0123456789abcdef" for c in body_hash.lower()):
                raise SKRSIError("invalid body hash")
        payload = {"natural_key": natural, "source": self.source, "cursor": cursor,
                   "value": 1, "unit": "metadata_event", "cohort": "estate",
                   "sample_id": natural, "collection_quality": item.get("quality", "complete"),
                   "metadata": metadata}
        for key in ("body_hash", "route", "evidence_ref", "cleanup_ref", "recovery_ref", "status"):
            if key in item: payload[key] = item[key]
        return natural, {"payload": payload, "cursor": cursor, "event_type": item.get("event_type", "skrsi.observation")}

    def drain(self, *, limit: int | None = None) -> CollectionResult:
        accepted = rejected = 0
        measurements: list[Measurement] = []
        count = self._queue.qsize() if limit is None else min(limit, self._queue.qsize())
        for _ in range(count):
            item = self._queue.get_nowait()
            try:
                natural, data = self._validate(item)
                digest = hashlib.sha256(str(data["payload"]).encode()).hexdigest()
                with self._lock:
                    if natural in self._seen:
                        if self._seen[natural] != digest: raise SKRSIError("natural key conflict")
                        self._metrics["duplicates"] += 1; continue
                    record = make_record("Observation", actor=self.source, target_ref=self.target_ref,
                                         payload=data["payload"], event_type=data["event_type"],
                                         event_id=str(item.get("event_id", natural)))
                    self.outbox.append(record)
                    self._seen[natural] = digest
                    if data["cursor"]: self._cursor = data["cursor"]
                    self._metrics["accepted"] += 1; accepted += 1
            except Exception:
                self._metrics["rejected"] += 1; self._metrics["malformed"] += 1; rejected += 1
        now = datetime.now(timezone.utc)
        measurements.append(Measurement("collector.events.accepted", accepted, self.target_revision, accepted, 0.0))
        measurements.append(Measurement("collector.events.rejected", rejected, self.target_revision, rejected, 0.0, quality="malformed"))
        measurements.append(Measurement("collector.queue.depth", self._queue.qsize(), self.target_revision, 1, 0.0,
                                        dimensions={"source": self.source}))
        measurements.append(Measurement("collector.cursor.freshness", 0.0, self.target_revision, accepted, 0.0,
                                        missing=not bool(self._cursor), quality="missing" if not self._cursor else "complete"))
        return CollectionResult(accepted, rejected, bool(self._metrics["overload"]), self._cursor, tuple(measurements))

    def metric_snapshot(self) -> tuple[Measurement, ...]:
        return self.drain(limit=0).measurements
