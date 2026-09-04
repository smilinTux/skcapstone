"""Tests for the FallbackTracker - graceful degradation logging."""

from __future__ import annotations

import json

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
# FallbackEvent - model tests
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
# FallbackTracker - happy path
# ---------------------------------------------------------------------------


class TestFallbackTrackerHappyPath:
    def test_record_and_load(self, tmp_path):
        """Record an event, then load it back."""
        tracker = FallbackTracker(root=tmp_path)
        evt = _event()
        tracker.record(evt)

        loaded = tracker.load_events()
        assert len(loaded) == 1
        assert loaded[0].primary_model == "gpt-4o"
        assert loaded[0].fallback_backend == "ollama"

    def test_multiple_events_newest_first(self, tmp_path):
        """load_events returns events newest-first."""
        tracker = FallbackTracker(root=tmp_path)
        tracker.record(_event(reason="first"))
        tracker.record(_event(reason="second"))
        tracker.record(_event(reason="third"))

        loaded = tracker.load_events()
        assert loaded[0].reason == "third"
        assert loaded[1].reason == "second"
        assert loaded[2].reason == "first"

    def test_limit_parameter(self, tmp_path):
        """limit= caps the returned events."""
        tracker = FallbackTracker(root=tmp_path)
        for i in range(5):
            tracker.record(_event(reason=f"event-{i}"))

        assert len(tracker.load_events(limit=2)) == 2
        assert len(tracker.load_events(limit=0)) == 5

    def test_writer_file_is_append_only_jsonl(self, tmp_path):
        """Each row is its own JSON line, so appending never rewrites history."""
        tracker = FallbackTracker(root=tmp_path)
        tracker.record(_event())
        tracker.record(_event(reason="second"))

        assert tracker.path.name.endswith(".jsonl")
        lines = [l for l in tracker.path.read_text().splitlines() if l.strip()]
        assert len(lines) == 2
        rows = [json.loads(line) for line in lines]
        assert all(isinstance(row, dict) for row in rows)
        assert rows[0]["primary_model"] == "gpt-4o"
        assert rows[1]["reason"] == "second"

    def test_success_and_failure_events(self, tmp_path):
        """success=True and success=False events are both stored."""
        tracker = FallbackTracker(root=tmp_path)
        tracker.record(_event(success=True, reason="worked"))
        tracker.record(_event(success=False, reason="failed"))

        events = tracker.load_events()
        successes = [e for e in events if e.success]
        failures = [e for e in events if not e.success]
        assert len(successes) == 1
        assert len(failures) == 1


# ---------------------------------------------------------------------------
# FallbackTracker - edge cases
# ---------------------------------------------------------------------------


class TestFallbackTrackerEdgeCases:
    def test_missing_file_returns_empty(self, tmp_path):
        """load_events on a non-existent file returns []."""
        tracker = FallbackTracker(root=tmp_path / "nonexistent")
        assert tracker.load_events() == []

    def test_corrupt_file_returns_empty(self, tmp_path):
        """A corrupt JSON file is treated as empty (no exception raised)."""
        path = tmp_path / "fallbacks.json"
        path.write_text("not valid json!!!", encoding="utf-8")

        tracker = FallbackTracker(root=tmp_path)
        assert tracker.load_events() == []

    def test_max_events_pruning(self, tmp_path):
        """Old events are pruned when max_events is exceeded."""
        tracker = FallbackTracker(root=tmp_path, max_events=3)
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
        tracker = FallbackTracker(root=tmp_path)
        for i in range(4):
            tracker.record(_event(reason=f"e{i}"))

        count = tracker.clear()
        assert count == 4
        assert tracker.load_events() == []

    def test_clear_on_empty_returns_zero(self, tmp_path):
        """clear() on an empty store returns 0."""
        tracker = FallbackTracker(root=tmp_path)
        assert tracker.clear() == 0

    def test_parent_dir_created_automatically(self, tmp_path):
        """Missing parent directories are created on first write."""
        nested = tmp_path / "a" / "b" / "c"
        tracker = FallbackTracker(root=nested)
        tracker.record(_event())
        assert tracker.path.exists()


# ---------------------------------------------------------------------------
# FallbackTracker - thread safety
# ---------------------------------------------------------------------------


class TestFallbackTrackerConcurrency:
    def test_concurrent_writes(self, tmp_path):
        """Concurrent record() calls from multiple threads don't corrupt the file."""
        import threading

        tracker = FallbackTracker(root=tmp_path)
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
        # max_events default is 1000, 4*10=40 events - all should be present
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


# ---------------------------------------------------------------------------
# Per-writer partitioning (prb-7810b08e: no shared read-modify-write)
# ---------------------------------------------------------------------------


class TestWriterPartitioning:
    def test_two_nodes_running_one_agent_never_share_a_file(self, tmp_path):
        """The replication-safety invariant: disjoint write sets per writer.

        The old layout re-read, mutated and rewrote one shared fallbacks.json,
        so two nodes overwrote each other and Syncthing conflicted. A bare
        <agent> filename would keep that bug for a fleet-wide agent, which is
        why the node is part of the writer identity.
        """
        a = FallbackTracker(root=tmp_path, agent="lumina", node="node-a")
        b = FallbackTracker(root=tmp_path, agent="lumina", node="node-b")

        a.record(_event(reason="from a"))
        b.record(_event(reason="from b"))

        assert a.path != b.path
        assert a.writer == "lumina@node-a"
        assert b.writer == "lumina@node-b"
        assert "from a" in a.path.read_text()
        assert "from a" not in b.path.read_text()
        # Either writer folds the whole directory on read.
        assert {e.reason for e in a.load_events()} == {"from a", "from b"}
        assert a.writers() == ["lumina@node-a", "lumina@node-b"]

    def test_trim_only_touches_the_owning_writer(self, tmp_path):
        """Rotation must never rewrite another node's rows."""
        a = FallbackTracker(root=tmp_path, agent="lumina", node="node-a", max_events=2)
        b = FallbackTracker(root=tmp_path, agent="lumina", node="node-b", max_events=2)
        b.record(_event(reason="b-keep"))
        for i in range(5):
            a.record(_event(reason=f"a-{i}"))

        assert len([l for l in a.path.read_text().splitlines() if l.strip()]) == 2
        assert [l for l in b.path.read_text().splitlines() if l.strip()]
        assert "b-keep" in {e.reason for e in b.load_events()}

    def test_events_from_all_writers_come_back_newest_first(self, tmp_path):
        a = FallbackTracker(root=tmp_path, agent="lumina", node="node-a")
        b = FallbackTracker(root=tmp_path, agent="opus", node="node-b")

        def at(reason, stamp):
            evt = _event(reason=reason)
            return FallbackEvent(**{**evt.model_dump(), "timestamp": stamp})

        a.record(at("oldest", "2026-01-01T00:00:00+00:00"))
        b.record(at("middle", "2026-06-01T00:00:00+00:00"))
        a.record(at("newest", "2026-09-01T00:00:00+00:00"))

        assert [e.reason for e in a.load_events()] == ["newest", "middle", "oldest"]
        assert [e.reason for e in a.load_events(limit=2)] == ["newest", "middle"]

    @pytest.mark.parametrize("agent,node", [("a/b", "n"), ("", ""), ("a b", "n d")])
    def test_writer_identity_is_always_one_safe_filename(self, tmp_path, agent, node):
        tracker = FallbackTracker(root=tmp_path, agent=agent, node=node)
        tracker.record(_event())
        assert "/" not in tracker.writer
        assert tracker.path.parent == tmp_path
        assert tracker.path.exists()

    def test_clear_removes_every_writer_file(self, tmp_path):
        a = FallbackTracker(root=tmp_path, agent="lumina", node="node-a")
        b = FallbackTracker(root=tmp_path, agent="lumina", node="node-b")
        a.record(_event())
        b.record(_event())

        assert a.clear() == 2
        assert a.load_events() == []
        assert a.writers() == []

    def test_migrate_legacy_list_into_a_writer_nobody_appends_to(self, tmp_path):
        """The old shared file was written by every node, so by no node."""
        from skcapstone.fallback_tracker import migrate_legacy_fallbacks

        legacy = tmp_path / "fallbacks.json"
        legacy.write_text(json.dumps([_event(reason="historical").model_dump()]))
        root = tmp_path / "fallbacks"

        assert migrate_legacy_fallbacks(legacy, root) == 1
        assert not legacy.exists()

        tracker = FallbackTracker(root=root, agent="lumina", node="node-a")
        assert "_legacy" in tracker.writers()
        assert "historical" in {e.reason for e in tracker.load_events()}
        # Idempotent, and the live writer stays separate from the legacy rows.
        assert migrate_legacy_fallbacks(legacy, root) == 0
        tracker.record(_event(reason="live"))
        assert tracker.path.stem == "lumina@node-a"
