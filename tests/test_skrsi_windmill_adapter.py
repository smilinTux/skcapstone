import json
from datetime import datetime, timezone

import pytest

from skcapstone.skrsi_registry import AppendOnlyOutbox
from skcapstone.skrsi_windmill_adapter import (
    WINDMILL_METADATA_KEYS,
    WindmillError,
    collect_completed_jobs,
    normalize_job,
)

NOW = datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc)
TARGET_REF = "target/windmill-estate"


def job(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "job-1",
        "started_at": "2026-09-08T19:00:00Z",
        "duration_ms": 123,
        "success": True,
        "script_path": "f/hammertime/inbox_drain",
        "schedule_path": "f/ops/notify",
    }
    value.update(changes)
    return value


def records(outbox: AppendOnlyOutbox) -> list[dict]:
    return [json.loads(entry.serialized) for entry in outbox.entries()]


def test_metadata_allowlist_includes_path_and_schedule():
    assert "path" in WINDMILL_METADATA_KEYS
    assert "schedule" in WINDMILL_METADATA_KEYS


def test_path_and_schedule_envelope_is_accepted():
    outbox = AppendOnlyOutbox()
    result, _ = collect_completed_jobs(outbox, [job()], target_ref=TARGET_REF, now=NOW)
    assert result.accepted == 1
    assert result.rejected == 0
    by_type: dict[str, list[dict]] = {}
    for record in records(outbox):
        by_type.setdefault(record["event_type"], []).append(record)
    # one throughput Observation plus the duration measurement
    assert len(by_type["windmill.job.completed"]) == 2
    assert len(by_type["windmill.job.success"]) == 1
    duration = next(
        record
        for record in by_type["windmill.job.completed"]
        if record["payload"].get("metric")
    )
    assert duration["payload"]["value"] == 123
    assert duration["payload"]["unit"] == "ms"
    success = by_type["windmill.job.success"][0]
    assert success["payload"]["value"] == 1
    # path/schedule are hashed dimensions, never persisted as free text
    throughput = next(
        record
        for record in by_type["windmill.job.completed"]
        if "metadata" in record["payload"]
    )
    metadata = throughput["payload"]["metadata"]
    assert metadata["path"].startswith("sha256:")
    assert metadata["schedule"].startswith("sha256:")


def test_replay_is_deduplicated():
    outbox = AppendOnlyOutbox()
    collect_completed_jobs(outbox, [job()], target_ref=TARGET_REF, now=NOW)
    before = len(outbox.entries())
    result, _ = collect_completed_jobs(outbox, [job()], target_ref=TARGET_REF, now=NOW)
    assert result.duplicates == 1
    assert len(outbox.entries()) == before


def test_malformed_job_fails_closed():
    with pytest.raises(WindmillError):
        normalize_job(job(duration_ms=None))
    with pytest.raises(WindmillError):
        normalize_job(job(success="yes"))
    with pytest.raises(WindmillError):
        normalize_job(job(started_at="not-a-timestamp"))


def test_cursor_regression_is_rejected():
    outbox = AppendOnlyOutbox()
    collect_completed_jobs(
        outbox,
        [job(started_at="2026-09-08T20:00:00Z")],
        target_ref=TARGET_REF,
        now=NOW,
    )
    result, _ = collect_completed_jobs(
        outbox,
        [job(started_at="2026-09-08T19:00:00Z")],
        target_ref=TARGET_REF,
        now=NOW,
    )
    assert result.accepted == 0
    assert result.rejected == 1
