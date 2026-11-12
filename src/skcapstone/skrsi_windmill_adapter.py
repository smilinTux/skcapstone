"""Fail-closed Windmill completed-job adapter for SKRSI.

This is a standalone source adapter. It pulls completed-job records from the
Windmill workspace API, normalizes each record, routes the job identity through
``BoundedCollector`` for the one-job-one-observation throughput count, and emits
two numeric measurements per job (duration and success). It never mutates the
Windmill store; its only durable output is the caller supplied append-only SKRSI
outbox.

Deliberately NOT wired into ``SKRSIRuntime.collect``: the collector and the
numeric measurement path are a single standalone adapter, so a job is ingested
exactly once rather than twice through two code paths (RV finding 5).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib import error as urlerror
from urllib import request

from .skrsi_collector import DEFAULT_METADATA_KEYS, BoundedCollector, CollectionResult
from .skrsi_registry import AppendOnlyOutbox, SKRSIError, make_record

#: The collector hashes every string metadata value, so script_path and
#: schedule_path are persisted only as sha256 correlation handles. They must
#: still be in the allowlist for the envelope to be admitted at all. Round 1 put
#: "path"/"schedule" in metadata without adding them here, so every envelope was
#: dead-lettered (RV finding 1). This set is the fix.
WINDMILL_METADATA_KEYS = frozenset(DEFAULT_METADATA_KEYS | {"path", "schedule"})

DURATION_METRIC = "skrsi.windmill.job.duration"
SUCCESS_METRIC = "skrsi.windmill.job.success"
ACTOR = "windmill-estate-adapter"
SOURCE = "windmill"


class WindmillError(SKRSIError):
    """A malformed or unavailable Windmill authority response."""


@dataclass(frozen=True)
class CompletedJob:
    """One normalized Windmill completed-job record."""

    id: str
    started_at: str
    duration_ms: int
    success: bool
    script_path: str
    schedule_path: str


def _utc_z(value: object, *, name: str) -> str:
    """Return a canonical ``Z``-suffixed UTC ISO-8601 timestamp.

    The collector orders non-decimal cursors lexically, so every cursor must
    use the same timezone suffix and a fixed width. Normalizing to microseconds
    here keeps ``started_at`` comparable across jobs even when Windmill returns
    a mix of ``Z`` and ``+00:00`` forms or varying fractional precision
    (RV finding 6).
    """
    if not isinstance(value, str):
        raise WindmillError(f"{name} must be an ISO-8601 timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WindmillError(f"{name} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise WindmillError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def normalize_job(job: Mapping[str, Any]) -> CompletedJob:
    """Validate and normalize one completed-job record, never guessing a value.

    Any malformed field raises ``WindmillError`` so the caller can fail closed
    instead of fabricating a healthy measurement.
    """
    job_id = job.get("id")
    if not isinstance(job_id, str) or not job_id.strip():
        raise WindmillError("job id is required")

    started_at = _utc_z(job.get("started_at") or job.get("created_at"), name="started_at")

    duration_ms = job.get("duration_ms")
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
        raise WindmillError("duration_ms must be a non-negative integer")

    success = job.get("success")
    if not isinstance(success, bool):
        raise WindmillError("success must be a boolean")

    script_path = job.get("script_path")
    if not isinstance(script_path, str) or not script_path.strip():
        raise WindmillError("script_path is required")

    schedule_path = job.get("schedule_path", "")
    if not isinstance(schedule_path, str):
        raise WindmillError("schedule_path must be a string")

    return CompletedJob(
        id=job_id.strip(),
        started_at=started_at,
        duration_ms=duration_ms,
        success=success,
        script_path=script_path,
        schedule_path=schedule_path,
    )


def fetch_completed_jobs(
    base_url: str,
    workspace: str,
    token: str,
    *,
    cursor: str = "",
    page_size: int = 50,
) -> tuple[list[dict[str, Any]], str | None]:
    """Pull one page of completed jobs, failing closed on any authority problem.

    The exact paginated response shape was not confirmed against a live
    Windmill superadmin token at proposal time; the field names and page
    envelope must be verified against one live response before running against
    production. Any non-200, malformed JSON, or missing page envelope raises
    ``WindmillError`` and records nothing.
    """
    url = (
        f"{base_url.rstrip('/')}/api/w/{workspace}/jobs/completed"
        f"?page=1&per_page={page_size}"
    )
    if cursor:
        url += f"&since={cursor}"
    req = request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
    except (urlerror.URLError, OSError, TimeoutError) as exc:
        raise WindmillError("windmill authority unavailable") from exc
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise WindmillError("windmill response is not JSON") from exc
    if not isinstance(data, Mapping):
        raise WindmillError("windmill response envelope is malformed")
    jobs = data.get("jobs") or data.get("results") or data.get("data")
    if not isinstance(jobs, list):
        raise WindmillError("windmill response has no job list")
    next_cursor = data.get("next_cursor") or data.get("next")
    if next_cursor is not None and not isinstance(next_cursor, str):
        raise WindmillError("windmill next cursor is malformed")
    return jobs, next_cursor


def _append_observation(
    outbox: AppendOnlyOutbox,
    *,
    event_id: str,
    event_type: str,
    occurred_at: str,
    recorded_at: str,
    value: int,
    unit: str,
    source: str,
    target_ref: str,
    revision: str,
    cohort: str,
    metric: str,
) -> str:
    """Emit one numeric Observation, deduped by idempotency key.

    Replaces the retired ``append_metric_event`` primitive (removed from main in
    the dispatcher retirement) without depending on that seat code. The value
    must be numeric and the idempotency key is derived from actor, event type,
    event id, and schema, so replays are skipped rather than duplicated.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise SKRSIError("metric value must be numeric")
    record = make_record(
        "Observation",
        actor=ACTOR,
        target_ref=target_ref,
        event_id=event_id,
        event_type=event_type,
        occurred_at=occurred_at,
        recorded_at=recorded_at,
        payload={
            "value": value,
            "unit": unit,
            "source": source,
            "cohort": cohort,
            "sample_id": event_id,
            "collection_quality": "complete",
            "metric": metric,
            "target_revision": revision,
        },
    )
    if not any(entry.idempotency_key == record.idempotency_key for entry in outbox.entries()):
        outbox.append(record)
    return record.idempotency_key


def collect_completed_jobs(
    outbox: AppendOnlyOutbox,
    jobs: list[Mapping[str, Any]],
    *,
    target_ref: str,
    target_revision: str = "1",
    now: datetime | None = None,
) -> tuple[CollectionResult, str]:
    """Normalize and collect one batch of completed jobs.

    Returns the collector result and the next cursor watermark (the maximum
    normalized ``started_at`` in the batch). Jobs are ordered by ``started_at``
    so the strictly increasing collector cursor fence holds within the batch.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")

    normalized = sorted((normalize_job(job) for job in jobs), key=lambda job: job.started_at)
    collector = BoundedCollector(
        outbox,
        source=SOURCE,
        target_ref=target_ref,
        authority="Windmill",
        handoff_owner="niobe",
        target_revision=target_revision,
        metadata_keys=WINDMILL_METADATA_KEYS,
    )

    now_iso = now.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    for job in normalized:
        envelope = {
            "natural_key": f"windmill:job:{job.id}",
            "source": SOURCE,
            "cursor": job.started_at,
            "occurred_at": job.started_at,
            "recorded_at": now_iso,
            "event_type": "windmill.job.completed",
            "metadata": {
                "duration_ms": job.duration_ms,
                "status": "success" if job.success else "failed",
                "path": job.script_path,
                "schedule": job.schedule_path,
            },
        }
        if not collector.submit(envelope):
            raise WindmillError("collector saturated; refusing to drop a completed job")

    result = collector.drain(now=now)

    for job in normalized:
        _append_observation(
            outbox,
            event_id=f"windmill:{job.id}:duration",
            event_type="windmill.job.completed",
            occurred_at=job.started_at,
            recorded_at=now_iso,
            value=job.duration_ms,
            unit="ms",
            source=SOURCE,
            target_ref=target_ref,
            revision=target_revision,
            cohort="windmill",
            metric=DURATION_METRIC,
        )
        _append_observation(
            outbox,
            event_id=f"windmill:{job.id}:success",
            event_type="windmill.job.success",
            occurred_at=job.started_at,
            recorded_at=now_iso,
            value=1 if job.success else 0,
            unit="1",
            source=SOURCE,
            target_ref=target_ref,
            revision=target_revision,
            cohort="windmill",
            metric=SUCCESS_METRIC,
        )

    next_cursor = normalized[-1].started_at if normalized else ""
    return result, next_cursor
