"""Tests for the agent mood tracker."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from skcapstone.mood import (
    MoodSnapshot,
    MoodTracker,
    _classify_social,
    _classify_stress,
    _classify_success,
    _compute_summary,
)


# ---------------------------------------------------------------------------
# Axis classifier unit tests
# ---------------------------------------------------------------------------


class TestClassifySuccess:
    """Unit tests for _classify_success."""

    def test_high_rate_is_happy(self) -> None:
        """>=90% success rate maps to 'happy'."""
        assert _classify_success(0.95) == "happy"
        assert _classify_success(1.0) == "happy"
        assert _classify_success(0.9) == "happy"

    def test_moderate_rate_is_content(self) -> None:
        """70–89% maps to 'content'."""
        assert _classify_success(0.80) == "content"
        assert _classify_success(0.70) == "content"

    def test_borderline_rate_is_neutral(self) -> None:
        """50–69% maps to 'neutral'."""
        assert _classify_success(0.60) == "neutral"
        assert _classify_success(0.50) == "neutral"

    def test_low_rate_is_frustrated(self) -> None:
        """<50% maps to 'frustrated'."""
        assert _classify_success(0.49) == "frustrated"
        assert _classify_success(0.0) == "frustrated"


class TestClassifySocial:
    """Unit tests for _classify_social."""

    def test_high_frequency_is_social(self) -> None:
        """>=10 msgs/hr maps to 'social'."""
        assert _classify_social(10.0) == "social"
        assert _classify_social(20.0) == "social"

    def test_medium_frequency_is_active(self) -> None:
        """3–9 msgs/hr maps to 'active'."""
        assert _classify_social(5.0) == "active"
        assert _classify_social(3.0) == "active"

    def test_low_frequency_is_quiet(self) -> None:
        """0.5–2 msgs/hr maps to 'quiet'."""
        assert _classify_social(1.0) == "quiet"
        assert _classify_social(0.5) == "quiet"

    def test_no_activity_is_isolated(self) -> None:
        """<0.5 msgs/hr maps to 'isolated'."""
        assert _classify_social(0.1) == "isolated"
        assert _classify_social(0.0) == "isolated"


class TestClassifyStress:
    """Unit tests for _classify_stress."""

    def test_very_low_errors_are_calm(self) -> None:
        """<5% error rate maps to 'calm'."""
        assert _classify_stress(0.0) == "calm"
        assert _classify_stress(0.04) == "calm"

    def test_low_errors_are_relaxed(self) -> None:
        """5–14% maps to 'relaxed'."""
        assert _classify_stress(0.10) == "relaxed"
        assert _classify_stress(0.05) == "relaxed"

    def test_moderate_errors_are_tense(self) -> None:
        """15–29% maps to 'tense'."""
        assert _classify_stress(0.20) == "tense"
        assert _classify_stress(0.15) == "tense"

    def test_high_errors_are_stressed(self) -> None:
        """>=30% maps to 'stressed'."""
        assert _classify_stress(0.30) == "stressed"
        assert _classify_stress(1.0) == "stressed"


class TestComputeSummary:
    """Unit tests for _compute_summary."""

    def test_stressed_overrides_all(self) -> None:
        """'stressed' dominates regardless of other axes."""
        assert _compute_summary("happy", "social", "stressed") == "stressed"

    def test_frustrated_overrides_non_stressed(self) -> None:
        """'frustrated' wins when stress is not 'stressed'."""
        assert _compute_summary("frustrated", "social", "calm") == "frustrated"
        assert _compute_summary("frustrated", "active", "relaxed") == "frustrated"

    def test_tense_follows_frustrated(self) -> None:
        """'tense' wins when not frustrated."""
        assert _compute_summary("content", "active", "tense") == "tense"

    def test_isolated_when_not_engaged(self) -> None:
        """Isolation surfaces when not otherwise stressed or frustrated."""
        assert _compute_summary("neutral", "isolated", "calm") == "isolated"

    def test_flourishing_when_happy_and_active(self) -> None:
        """Happy + socially active → 'flourishing'."""
        assert _compute_summary("happy", "social", "calm") == "flourishing"
        assert _compute_summary("happy", "active", "calm") == "flourishing"

    def test_happy_without_social(self) -> None:
        """Happy but quiet stays 'happy'."""
        assert _compute_summary("happy", "quiet", "calm") == "happy"

    def test_content_maps_to_content(self) -> None:
        """content + quiet + calm → 'content'."""
        assert _compute_summary("content", "quiet", "calm") == "content"

    def test_fallback_is_neutral(self) -> None:
        """neutral + quiet + calm → 'neutral'."""
        assert _compute_summary("neutral", "quiet", "calm") == "neutral"


# ---------------------------------------------------------------------------
# MoodTracker integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def tracker(tmp_path: Path) -> MoodTracker:
    """MoodTracker using a temp home directory."""
    return MoodTracker(home=tmp_path)


class TestMoodTrackerUpdate:
    """Tests for MoodTracker.update()."""

    def test_happy_high_success(self, tracker: MoodTracker) -> None:
        """High response rate produces 'happy' success_mood."""
        snap = tracker.update(messages=100, responses=95, errors=0)
        assert snap.success_mood == "happy"
        assert snap.summary in ("happy", "flourishing")

    def test_frustrated_low_success(self, tracker: MoodTracker) -> None:
        """Low response rate produces 'frustrated' success_mood and summary."""
        snap = tracker.update(messages=100, responses=10, errors=0)
        assert snap.success_mood == "frustrated"
        assert snap.summary == "frustrated"

    def test_stressed_high_errors(self, tracker: MoodTracker) -> None:
        """High error rate produces 'stressed' and overrides success in summary."""
        snap = tracker.update(messages=100, responses=90, errors=40)
        assert snap.stress_mood == "stressed"
        assert snap.summary == "stressed"

    def test_calm_low_errors(self, tracker: MoodTracker) -> None:
        """Near-zero errors produce 'calm' stress mood."""
        snap = tracker.update(messages=50, responses=49, errors=1)
        assert snap.stress_mood == "calm"

    def test_social_high_frequency(self, tracker: MoodTracker) -> None:
        """Many messages in a short window → 'social'."""
        # 100 messages over 1 hour = 100 msgs/hr
        snap = tracker.update(messages=100, responses=90, errors=0, window_hours=1)
        assert snap.social_mood == "social"

    def test_isolated_no_messages(self, tracker: MoodTracker) -> None:
        """Few messages in a long window → 'isolated'."""
        snap = tracker.update(messages=2, responses=2, errors=0, window_hours=24)
        assert snap.social_mood == "isolated"

    def test_zero_messages_defaults_to_neutral(self, tracker: MoodTracker) -> None:
        """Zero messages produce safe default rates (no division by zero)."""
        snap = tracker.update(messages=0, responses=0, errors=0)
        assert snap.success_rate == 1.0
        assert snap.error_rate == 0.0
        assert snap.summary in ("neutral", "isolated")  # isolated because 0 msgs/hr

    def test_rates_are_clamped_to_four_decimals(self, tracker: MoodTracker) -> None:
        """success_rate and error_rate are rounded to 4 decimal places."""
        snap = tracker.update(messages=3, responses=2, errors=1)
        # 2/3 ≈ 0.6667, 1/3 ≈ 0.3333
        assert snap.success_rate == round(2 / 3, 4)
        assert snap.error_rate == round(1 / 3, 4)

    def test_updated_at_is_set(self, tracker: MoodTracker) -> None:
        """updated_at is populated after update."""
        snap = tracker.update(messages=5, responses=5, errors=0)
        assert snap.updated_at != ""
        assert "T" in snap.updated_at  # ISO-8601 format


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestMoodPersistence:
    """Tests for save / load round-trip."""

    def test_update_persists_file(self, tmp_path: Path) -> None:
        """update() writes mood.json to the home directory."""
        tracker = MoodTracker(home=tmp_path)
        tracker.update(messages=10, responses=9, errors=0)
        assert (tmp_path / "mood.json").exists()

    def test_reload_recovers_state(self, tmp_path: Path) -> None:
        """A second MoodTracker in the same home loads the saved snapshot."""
        t1 = MoodTracker(home=tmp_path)
        t1.update(messages=20, responses=18, errors=1)

        t2 = MoodTracker(home=tmp_path)
        snap = t2.snapshot
        assert snap.messages_processed == 20
        assert snap.responses_sent == 18
        assert snap.errors == 1

    def test_corrupt_file_yields_neutral(self, tmp_path: Path) -> None:
        """Corrupt mood.json is silently ignored; tracker starts neutral."""
        mood_path = tmp_path / "mood.json"
        mood_path.write_text("not valid json {{{", encoding="utf-8")
        tracker = MoodTracker(home=tmp_path)
        snap = tracker.snapshot
        assert snap.summary == "neutral"

    def test_missing_file_yields_neutral(self, tmp_path: Path) -> None:
        """Absent mood.json yields a neutral default snapshot."""
        tracker = MoodTracker(home=tmp_path / "nonexistent")
        snap = tracker.snapshot
        assert snap.summary == "neutral"


# ---------------------------------------------------------------------------
# update_from_metrics
# ---------------------------------------------------------------------------


class TestUpdateFromMetrics:
    """Tests for MoodTracker.update_from_metrics()."""

    def test_reads_consciousness_metrics(self, tmp_path: Path) -> None:
        """update_from_metrics reads from ConsciousnessMetrics.to_dict()."""
        from skcapstone.metrics import ConsciousnessMetrics

        cm = ConsciousnessMetrics(home=tmp_path, persist_interval=0)
        for _ in range(5):
            cm.record_message("peer-a")
        for _ in range(4):
            cm.record_response(50.0, "ollama", "fast")
        cm.record_error()

        tracker = MoodTracker(home=tmp_path)
        snap = tracker.update_from_metrics(cm)
        assert snap.messages_processed == 5
        assert snap.responses_sent == 4
        assert snap.errors == 1

    def test_bad_metrics_object_returns_current_snapshot(self, tmp_path: Path) -> None:
        """update_from_metrics with a broken object returns existing snapshot."""

        class _BrokenMetrics:
            def to_dict(self):
                raise RuntimeError("broken")

        tracker = MoodTracker(home=tmp_path)
        snap_before = tracker.snapshot
        snap_after = tracker.update_from_metrics(_BrokenMetrics())
        assert snap_after.summary == snap_before.summary


# ---------------------------------------------------------------------------
# load_snapshot classmethod
# ---------------------------------------------------------------------------


class TestLoadSnapshot:
    """Tests for MoodTracker.load_snapshot()."""

    def test_returns_default_when_no_file(self, tmp_path: Path) -> None:
        """Returns a neutral MoodSnapshot when no file exists."""
        snap = MoodTracker.load_snapshot(home=tmp_path)
        assert isinstance(snap, MoodSnapshot)
        assert snap.summary == "neutral"

    def test_returns_saved_snapshot(self, tmp_path: Path) -> None:
        """Returns the persisted snapshot when mood.json exists."""
        t = MoodTracker(home=tmp_path)
        t.update(messages=30, responses=28, errors=0)
        snap = MoodTracker.load_snapshot(home=tmp_path)
        assert snap.messages_processed == 30


# ---------------------------------------------------------------------------
# describe()
# ---------------------------------------------------------------------------


class TestDescribe:
    """Tests for MoodTracker.describe()."""

    def test_describe_contains_summary(self, tracker: MoodTracker) -> None:
        """describe() includes the summary word."""
        tracker.update(messages=10, responses=10, errors=0)
        text = tracker.describe()
        assert "Mood summary" in text

    def test_describe_contains_all_axes(self, tracker: MoodTracker) -> None:
        """describe() mentions all three mood axes."""
        tracker.update(messages=10, responses=9, errors=0)
        text = tracker.describe()
        assert "Success" in text
        assert "Social" in text
        assert "Stress" in text

    def test_describe_contains_updated_at(self, tracker: MoodTracker) -> None:
        """describe() includes the updated_at timestamp."""
        tracker.update(messages=5, responses=5, errors=0)
        text = tracker.describe()
        assert "Updated" in text


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    """Tests for concurrent MoodTracker access."""

    def test_concurrent_updates_are_safe(self, tmp_path: Path) -> None:
        """Concurrent update() calls do not raise exceptions."""
        tracker = MoodTracker(home=tmp_path)
        errors: list[Exception] = []

        def _work(i: int) -> None:
            try:
                tracker.update(messages=i + 1, responses=i, errors=0)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_work, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Unexpected errors: {errors}"
        # Snapshot must still be a valid MoodSnapshot
        snap = tracker.snapshot
        assert isinstance(snap, MoodSnapshot)


# ---------------------------------------------------------------------------
# MoodSnapshot model
# ---------------------------------------------------------------------------


class TestMoodSnapshot:
    """Tests for MoodSnapshot model."""

    def test_defaults_are_neutral(self) -> None:
        """Default snapshot is neutral / quiet / calm."""
        snap = MoodSnapshot()
        assert snap.summary == "neutral"
        assert snap.success_mood == "neutral"
        assert snap.social_mood == "quiet"
        assert snap.stress_mood == "calm"

    def test_json_serializable(self) -> None:
        """MoodSnapshot serializes to valid JSON."""
        snap = MoodSnapshot(
            messages_processed=5,
            responses_sent=5,
            errors=0,
            summary="happy",
            updated_at="2026-03-02T12:00:00+00:00",
        )
        data = snap.model_dump_json()
        parsed = json.loads(data)
        assert parsed["summary"] == "happy"
        assert parsed["messages_processed"] == 5
