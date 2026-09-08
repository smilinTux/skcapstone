from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from skcapstone.skrsi_collector import BoundedCollector
from skcapstone.skrsi_registry import AppendOnlyOutbox

NOW = datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc)


def event(
    natural_key: str = "event-1",
    *,
    cursor: str = "000001",
    occurred_at: datetime = NOW,
    **changes: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "source": "cardstore",
        "natural_key": natural_key,
        "event_id": natural_key,
        "event_type": "skrsi.card",
        "cursor": cursor,
        "occurred_at": occurred_at.isoformat(),
        "recorded_at": occurred_at.isoformat(),
        "metadata": {"state": "done"},
        "body_hash": "a" * 64,
    }
    value.update(changes)
    return value


def collector(outbox: AppendOnlyOutbox, **changes: object) -> BoundedCollector:
    return BoundedCollector(
        outbox,
        source="cardstore",
        target_ref="target/software-lifecycle",
        authority="CardStore",
        handoff_owner="atlas",
        **changes,
    )


def test_restart_dedup_cursor_and_overload(tmp_path):
    path = tmp_path / "outbox.jsonl"
    first = collector(AppendOnlyOutbox(path), queue_size=1)
    assert first.submit(event())
    assert not first.submit(event("event-2", cursor="000002"))
    result = first.drain(now=NOW)
    assert (result.accepted, result.overloaded, result.cursor) == (1, True, "000001")

    restarted = collector(AppendOnlyOutbox(path))
    assert restarted.cursor == "000001"
    assert restarted.submit(event())
    assert restarted.submit(event("event-0", cursor="000000"))
    result = restarted.drain(now=NOW)
    assert (result.accepted, result.duplicates, result.rejected) == (0, 1, 1)


def test_protected_value_becomes_hash_only_dead_letter(tmp_path):
    outbox = AppendOnlyOutbox(tmp_path / "outbox.jsonl")
    worker = collector(outbox)
    protected = event(metadata={"nested": {"api_token": "never-persist-me"}})
    assert worker.submit(protected)
    assert worker.drain(now=NOW).rejected == 1
    persisted = (tmp_path / "outbox.jsonl").read_text()
    assert "never-persist-me" not in persisted
    assert "api_token" not in persisted
    assert "skrsi.collection_error" in persisted


@pytest.mark.parametrize(
    "value",
    [
        event(occurred_at=NOW - timedelta(days=396)),
        event(occurred_at=NOW + timedelta(minutes=6)),
        event(occurred_at=NOW, recorded_at=(NOW - timedelta(seconds=1)).isoformat()),
    ],
)
def test_invalid_time_is_rejected(value):
    worker = collector(AppendOnlyOutbox())
    assert worker.submit(value)
    assert worker.drain(now=NOW).rejected == 1


def test_natural_key_conflict_is_rejected():
    worker = collector(AppendOnlyOutbox())
    assert worker.submit(event())
    assert worker.drain(now=NOW).accepted == 1
    assert worker.submit(event(metadata={"state": "review"}))
    assert worker.drain(now=NOW).rejected == 1


def test_concurrent_replay_persists_exactly_once():
    outbox = AppendOnlyOutbox()
    worker = collector(outbox, queue_size=64)
    with ThreadPoolExecutor(max_workers=16) as pool:
        assert all(pool.map(worker.submit, [event()] * 64))
    result = worker.drain(now=NOW)
    assert (result.accepted, result.duplicates, result.rejected) == (1, 63, 0)
    records = [entry for entry in outbox.entries() if b"skrsi.card" in entry.serialized]
    assert len(records) == 1


def test_ten_thousand_event_capacity_without_silent_loss():
    worker = collector(AppendOnlyOutbox(), queue_size=10_000)
    for index in range(10_000):
        assert worker.submit(event(f"event-{index}", cursor=f"{index:06d}"))
    assert not worker.submit(event("overflow", cursor="010000"))
    result = worker.drain(now=NOW)
    assert (result.accepted, result.rejected, result.overloaded) == (10_000, 0, True)
    assert result.cursor == "009999"


@pytest.mark.parametrize(
    "value",
    [
        {"source": "cardstore", "natural_key": "missing-time", "cursor": "1"},
        event(source="other"),
        event(body_hash="bad"),
        event(metadata={"password": "hidden"}),
    ],
)
def test_malformed_or_unauthorized_event_is_rejected(value):
    worker = collector(AppendOnlyOutbox())
    assert worker.submit(value)
    assert worker.drain(now=NOW).rejected == 1


def test_measurements_include_quality_freshness_and_bounded_dimensions():
    worker = collector(AppendOnlyOutbox())
    missing = worker.metric_snapshot(now=NOW)
    assert all(metric.missing and metric.quality == "missing" for metric in missing)
    assert all(
        set(metric.dimensions) == {"source", "authority", "handoff_owner"} for metric in missing
    )

    assert worker.submit(event(occurred_at=NOW - timedelta(seconds=3)))
    result = worker.drain(now=NOW)
    assert result.measurements
    assert all(metric.target_revision == "1" for metric in result.measurements)
    assert all(metric.sample_count == 1 for metric in result.measurements)
    assert all(metric.freshness_seconds == 3 for metric in result.measurements)
