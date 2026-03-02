"""Tests for the FallbackTracker — graceful degradation logging."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.fallback_tracker import FallbackEvent, FallbackTracker, get_tracker


def _event(
    primary="gpt-4o",
    primary_backend="openai",
    fallback_model="llama3.2",
    fallback_backend="ollama",
    reason="primary failed",
    success=True,
) -> FallbackEvent:
    return FallbackEvent(
        primary_model=primary,
        primary_backend=primary_backend,
        fallback_model=fallback_model,
        fallback_backend=fallback_backend,
        reason=reason,
        success=success,
    )


# ---------------------------------------------------------------------------
# FallbackEvent — model tests
# ---------------------------------------------------------------------------


class TestFallbackEvent:
    def test_defaults_set_timestamp(self):
        """Timestamp is populated automatically."""
        evt = _event()
        assert evt.timestamp  # non-empty string
        assert "T" in evt.timestamp  # ISO format

    def test_fields_round_trip(self):
        """model_dump() and re-instantiation preserve all fields."""
        evt = _event(reason="timeout", success=False)
        dumped = evt.model_dump()
        restored = FallbackEvent(**dumped)
        assert restored.reason == "timeout"
        assert restored.success is False
        assert restored.primary_model == "gpt-4o"


# ---------------------------------------------------------------------------
# FallbackTracker — happy path
# ---------------------------------------------------------------------------


class TestFallbackTrackerHappyPath:
    def test_record_and_load(self, tmp_path):
        """Record an event, then load it back."""
        tracker = FallbackTracker(path=tmp_path / "fallbacks.json")
        evt = _event()
        tracker.record(evt)

        loaded = tracker.load_events()
        assert len(loaded) == 1
        assert loaded[0].primary_model == "gpt-4o"
        assert loaded[0].fallback_backend == "ollama"

    def test_multiple_events_newest_first(self, tmp_path):
        """load_events returns events newest-first."""
        tracker = FallbackTracker(path=tmp_path / "fallbacks.json")
        tracker.record(_event(reason="first"))
        tracker.record(_event(reason="second"))
        tracker.record(_event(reason="third"))

        loaded = tracker.load_events()
        assert loaded[0].reason == "third"
        assert loaded[1].reason == "second"
        assert loaded[2].reason == "first"

    def test_limit_parameter(self, tmp_path):
        """limit= caps the returned events."""
        tracker = FallbackTracker(path=tmp_path / "fallbacks.json")
        for i in range(5):
            tracker.record(_event(reason=f"event-{i}"))

        assert len(tracker.load_events(limit=2)) == 2
        assert len(tracker.load_events(limit=0)) == 5

    def test_file_is_valid_json(self, tmp_path):
        """The written file is valid JSON list."""
        path = tmp_path / "fallbacks.json"
        tracker = FallbackTracker(path=path)
        tracker.record(_event())

        data = json.loads(path.read_text())
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["primary_model"] == "gpt-4o"

    def test_success_and_failure_events(self, tmp_path):
        """success=True and success=False events are both stored."""
        tracker = FallbackTracker(path=tmp_path / "fallbacks.json")
        tracker.record(_event(success=True, reason="worked"))
        tracker.record(_event(success=False, reason="failed"))

        events = tracker.load_events()
        successes = [e for e in events if e.success]
        failures = [e for e in events if not e.success]
        assert len(successes) == 1
        assert len(failures) == 1


# ---------------------------------------------------------------------------
# FallbackTracker — edge cases
# ---------------------------------------------------------------------------


class TestFallbackTrackerEdgeCases:
    def test_missing_file_returns_empty(self, tmp_path):
        """load_events on a non-existent file returns []."""
        tracker = FallbackTracker(path=tmp_path / "nonexistent.json")
        assert tracker.load_events() == []

    def test_corrupt_file_returns_empty(self, tmp_path):
        """A corrupt JSON file is treated as empty (no exception raised)."""
        path = tmp_path / "fallbacks.json"
        path.write_text("not valid json!!!", encoding="utf-8")

        tracker = FallbackTracker(path=path)
        assert tracker.load_events() == []

    def test_max_events_pruning(self, tmp_path):
        """Old events are pruned when max_events is exceeded."""
        tracker = FallbackTracker(path=tmp_path / "fallbacks.json", max_events=3)
        for i in range(5):
            tracker.record(_event(reason=f"e{i}"))

        events = tracker.load_events()
        assert len(events) == 3
        # Newest three should be retained (newest-first order)
        reasons = [e.reason for e in events]
        assert "e4" in reasons
        assert "e3" in reasons
        assert "e2" in reasons
        assert "e0" not in reasons

    def test_clear_removes_all_events(self, tmp_path):
        """clear() deletes all events and returns count."""
        tracker = FallbackTracker(path=tmp_path / "fallbacks.json")
        for i in range(4):
            tracker.record(_event(reason=f"e{i}"))

        count = tracker.clear()
        assert count == 4
        assert tracker.load_events() == []

    def test_clear_on_empty_returns_zero(self, tmp_path):
        """clear() on an empty store returns 0."""
        tracker = FallbackTracker(path=tmp_path / "fallbacks.json")
        assert tracker.clear() == 0

    def test_parent_dir_created_automatically(self, tmp_path):
        """Missing parent directories are created on first write."""
        nested = tmp_path / "a" / "b" / "c" / "fallbacks.json"
        tracker = FallbackTracker(path=nested)
        tracker.record(_event())
        assert nested.exists()


# ---------------------------------------------------------------------------
# FallbackTracker — thread safety
# ---------------------------------------------------------------------------


class TestFallbackTrackerConcurrency:
    def test_concurrent_writes(self, tmp_path):
        """Concurrent record() calls from multiple threads don't corrupt the file."""
        import threading

        tracker = FallbackTracker(path=tmp_path / "fallbacks.json")
        errors: list[Exception] = []

        def write_events():
            try:
                for i in range(10):
                    tracker.record(_event(reason=f"thread-{i}"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=write_events) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
        events = tracker.load_events()
        # max_events default is 1000, 4*10=40 events — all should be present
        assert len(events) == 40


# ---------------------------------------------------------------------------
# get_tracker singleton
# ---------------------------------------------------------------------------


class TestGetTrackerSingleton:
    def test_same_instance_returned(self):
        """get_tracker() returns the same object on repeated calls."""
        t1 = get_tracker()
        t2 = get_tracker()
        assert t1 is t2

    def test_singleton_is_fallback_tracker(self):
        """Singleton is a FallbackTracker instance."""
        tracker = get_tracker()
        assert isinstance(tracker, FallbackTracker)
