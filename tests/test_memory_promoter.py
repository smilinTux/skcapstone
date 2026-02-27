"""Tests for Memory Auto-Promotion Engine."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skcapstone.memory_promoter import (
    PromotionEngine,
    PromotionThresholds,
    SweepResult,
)
from skcapstone.models import MemoryEntry, MemoryLayer


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Create a minimal agent home for promoter tests."""
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    for layer in ("short-term", "mid-term", "long-term"):
        (mem_dir / layer).mkdir()
    return tmp_path


@pytest.fixture
def engine(home: Path) -> PromotionEngine:
    """Create a PromotionEngine with default thresholds."""
    return PromotionEngine(home)


def _write_memory(
    home: Path,
    memory_id: str,
    content: str = "Test memory",
    layer: MemoryLayer = MemoryLayer.SHORT_TERM,
    importance: float = 0.5,
    access_count: int = 0,
    tags: list[str] | None = None,
    age_hours: float = 0,
) -> MemoryEntry:
    """Helper to create a memory file on disk."""
    created_at = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    entry = MemoryEntry(
        memory_id=memory_id,
        content=content,
        tags=tags or [],
        source="test",
        layer=layer,
        importance=importance,
        access_count=access_count,
        created_at=created_at,
    )
    path = home / "memory" / layer.value / f"{memory_id}.json"
    path.write_text(entry.model_dump_json(indent=2), encoding="utf-8")
    return entry


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


class TestScoring:
    """Tests for the scoring system."""

    def test_high_importance_scores_high(self, engine: PromotionEngine, home: Path) -> None:
        """High importance memories score higher."""
        entry = _write_memory(home, "imp-high", importance=0.95, age_hours=48)
        candidate = engine.score(entry)
        assert candidate.signals["importance"] == 0.95

    def test_low_importance_scores_low(self, engine: PromotionEngine, home: Path) -> None:
        """Low importance memories score lower."""
        entry = _write_memory(home, "imp-low", importance=0.1, age_hours=48)
        candidate = engine.score(entry)
        assert candidate.signals["importance"] == 0.1

    def test_emotional_tags_boost_score(self, engine: PromotionEngine, home: Path) -> None:
        """Emotional tags increase the emotion signal."""
        entry = _write_memory(
            home, "emo",
            tags=["love", "trust", "cloud9"],
            age_hours=48,
        )
        candidate = engine.score(entry)
        assert candidate.signals["emotion"] == 1.0

    def test_no_emotional_content(self, engine: PromotionEngine, home: Path) -> None:
        """No emotional tags or content yields zero emotion score."""
        entry = _write_memory(home, "noemo", tags=["debug", "test"], age_hours=48)
        candidate = engine.score(entry)
        assert candidate.signals["emotion"] == 0.0

    def test_high_access_frequency(self, engine: PromotionEngine, home: Path) -> None:
        """Frequently accessed memories score high on access."""
        entry = _write_memory(home, "freq", access_count=10, age_hours=1)
        candidate = engine.score(entry)
        assert candidate.signals["access"] == 1.0

    def test_no_access(self, engine: PromotionEngine, home: Path) -> None:
        """Never-accessed memories score zero on access."""
        entry = _write_memory(home, "noaccess", access_count=0, age_hours=48)
        candidate = engine.score(entry)
        assert candidate.signals["access"] == 0.0

    def test_age_maturity_short_term(self, engine: PromotionEngine, home: Path) -> None:
        """Short-term memories older than 24h start scoring on age."""
        young = _write_memory(home, "young", importance=0.8, age_hours=12)
        old = _write_memory(home, "old", importance=0.8, age_hours=48)
        young_c = engine.score(young)
        old_c = engine.score(old)
        assert old_c.signals["age"] > young_c.signals["age"]

    def test_tag_richness(self, engine: PromotionEngine, home: Path) -> None:
        """Well-tagged memories score higher on tags."""
        rich = _write_memory(
            home, "rich",
            tags=["a", "b", "c", "d", "e"],
            age_hours=48,
        )
        poor = _write_memory(home, "poor", tags=[], age_hours=48)
        rich_c = engine.score(rich)
        poor_c = engine.score(poor)
        assert rich_c.signals["tags"] == 1.0
        assert poor_c.signals["tags"] == 0.0

    def test_score_clamped_to_unit(self, engine: PromotionEngine, home: Path) -> None:
        """Score is always between 0 and 1."""
        entry = _write_memory(
            home, "extreme",
            importance=1.0,
            access_count=100,
            tags=["love", "trust", "cloud9", "milestone", "breakthrough"],
            age_hours=200,
        )
        candidate = engine.score(entry)
        assert 0.0 <= candidate.score <= 1.0

    def test_target_layer_short_to_mid(self, engine: PromotionEngine, home: Path) -> None:
        """Short-term memories target mid-term."""
        entry = _write_memory(home, "s2m", layer=MemoryLayer.SHORT_TERM)
        candidate = engine.score(entry)
        assert candidate.target_layer == "mid-term"

    def test_target_layer_mid_to_long(self, engine: PromotionEngine, home: Path) -> None:
        """Mid-term memories target long-term."""
        entry = _write_memory(home, "m2l", layer=MemoryLayer.MID_TERM)
        candidate = engine.score(entry)
        assert candidate.target_layer == "long-term"


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


class TestSweep:
    """Tests for the promotion sweep."""

    def test_sweep_empty(self, engine: PromotionEngine, home: Path) -> None:
        """Sweep on empty memory returns empty result."""
        result = engine.sweep()
        assert result.scanned == 0
        assert result.promoted == []

    def test_sweep_promotes_qualifying(self, home: Path) -> None:
        """Qualifying memories are promoted."""
        # Low thresholds so our test memory qualifies
        engine = PromotionEngine(
            home,
            thresholds=PromotionThresholds(short_to_mid=0.3),
        )
        _write_memory(
            home, "promote-me",
            importance=0.9,
            access_count=5,
            tags=["milestone", "love"],
            age_hours=48,
        )

        result = engine.sweep()
        assert len(result.promoted) >= 1
        assert result.promoted[0].memory_id == "promote-me"

        # File should have moved
        assert not (home / "memory" / "short-term" / "promote-me.json").exists()
        assert (home / "memory" / "mid-term" / "promote-me.json").exists()

    def test_sweep_skips_unqualified(self, home: Path) -> None:
        """Low-scoring memories are not promoted."""
        engine = PromotionEngine(
            home,
            thresholds=PromotionThresholds(short_to_mid=0.9),
        )
        _write_memory(home, "skip-me", importance=0.1, age_hours=1)

        result = engine.sweep()
        assert len(result.promoted) == 0
        assert result.skipped == 1

    def test_sweep_dry_run(self, home: Path) -> None:
        """Dry run doesn't move files."""
        engine = PromotionEngine(
            home,
            thresholds=PromotionThresholds(short_to_mid=0.1),
        )
        _write_memory(
            home, "dry-run",
            importance=0.9,
            access_count=10,
            age_hours=48,
        )

        result = engine.sweep(dry_run=True)
        assert result.dry_run is True
        # File should still be in short-term
        assert (home / "memory" / "short-term" / "dry-run.json").exists()

    def test_sweep_respects_limit(self, home: Path) -> None:
        """Limit caps the number of promotions."""
        engine = PromotionEngine(
            home,
            thresholds=PromotionThresholds(short_to_mid=0.1),
        )
        for i in range(5):
            _write_memory(
                home, f"limit{i:02d}",
                importance=0.9,
                access_count=10,
                age_hours=48,
            )

        result = engine.sweep(limit=2)
        assert len(result.promoted) == 2

    def test_sweep_single_layer(self, home: Path) -> None:
        """Sweep restricted to a single layer."""
        engine = PromotionEngine(
            home,
            thresholds=PromotionThresholds(short_to_mid=0.1, mid_to_long=0.1),
        )
        _write_memory(
            home, "short1",
            importance=0.9, access_count=10, age_hours=48,
            layer=MemoryLayer.SHORT_TERM,
        )
        _write_memory(
            home, "mid1",
            importance=0.9, access_count=10, age_hours=200,
            layer=MemoryLayer.MID_TERM,
        )

        result = engine.sweep(layer=MemoryLayer.SHORT_TERM)
        promoted_ids = [c.memory_id for c in result.promoted]
        assert "short1" in promoted_ids
        assert "mid1" not in promoted_ids

    def test_sweep_records_layer_counts(self, home: Path) -> None:
        """Sweep result includes layer counts after sweep."""
        engine = PromotionEngine(home)
        _write_memory(home, "count1")
        _write_memory(home, "count2")
        result = engine.sweep()
        assert "short-term" in result.by_layer

    def test_long_term_not_swept(self, home: Path) -> None:
        """Long-term memories are never swept for promotion."""
        engine = PromotionEngine(
            home,
            thresholds=PromotionThresholds(mid_to_long=0.0),
        )
        _write_memory(
            home, "longterm",
            layer=MemoryLayer.LONG_TERM,
            importance=1.0,
            access_count=100,
            age_hours=1000,
        )
        result = engine.sweep()
        assert result.scanned == 0  # Long-term not scanned


# ---------------------------------------------------------------------------
# Mid-term to long-term
# ---------------------------------------------------------------------------


class TestMidToLong:
    """Tests for mid-term to long-term promotion."""

    def test_mid_to_long_promotion(self, home: Path) -> None:
        """Mid-term memories can be promoted to long-term."""
        engine = PromotionEngine(
            home,
            thresholds=PromotionThresholds(mid_to_long=0.3),
        )
        _write_memory(
            home, "m2l-promo",
            layer=MemoryLayer.MID_TERM,
            importance=0.9,
            access_count=15,
            tags=["milestone", "architecture"],
            age_hours=200,
        )

        result = engine.sweep()
        assert len(result.promoted) >= 1
        assert (home / "memory" / "long-term" / "m2l-promo.json").exists()

    def test_mid_needs_higher_threshold(self, home: Path) -> None:
        """Mid-term promotion requires a higher score than short-term."""
        thresholds = PromotionThresholds()
        assert thresholds.mid_to_long > thresholds.short_to_mid


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


class TestHistory:
    """Tests for promotion history logging."""

    def test_sweep_records_history(self, engine: PromotionEngine, home: Path) -> None:
        """Sweep writes to promotion-log.json."""
        engine.sweep()
        log_path = home / "memory" / "promotion-log.json"
        assert log_path.exists()

    def test_history_contains_sweep_data(self, engine: PromotionEngine, home: Path) -> None:
        """History entries contain sweep metadata."""
        _write_memory(home, "hist1")
        engine.sweep()

        history = engine.get_history()
        assert len(history) >= 1
        entry = history[-1]
        assert "timestamp" in entry
        assert "scanned" in entry
        assert "promoted" in entry

    def test_history_accumulates(self, engine: PromotionEngine, home: Path) -> None:
        """Multiple sweeps accumulate in history."""
        engine.sweep()
        engine.sweep()
        engine.sweep()

        history = engine.get_history()
        assert len(history) >= 3

    def test_get_history_empty(self, engine: PromotionEngine) -> None:
        """Get history on fresh instance returns empty."""
        assert engine.get_history() == []

    def test_get_history_limit(self, engine: PromotionEngine, home: Path) -> None:
        """History respects limit parameter."""
        for _ in range(5):
            engine.sweep()

        history = engine.get_history(limit=2)
        assert len(history) == 2


# ---------------------------------------------------------------------------
# Custom Thresholds
# ---------------------------------------------------------------------------


class TestCustomThresholds:
    """Tests for configurable thresholds."""

    def test_strict_thresholds_reject_all(self, home: Path) -> None:
        """Very high thresholds reject everything."""
        engine = PromotionEngine(
            home,
            thresholds=PromotionThresholds(short_to_mid=1.0, mid_to_long=1.0),
        )
        _write_memory(home, "strict", importance=0.5, age_hours=48)
        result = engine.sweep()
        assert len(result.promoted) == 0

    def test_lenient_thresholds_promote_all(self, home: Path) -> None:
        """Very low thresholds promote everything."""
        engine = PromotionEngine(
            home,
            thresholds=PromotionThresholds(short_to_mid=0.0),
        )
        _write_memory(home, "lenient1", age_hours=48)
        _write_memory(home, "lenient2", age_hours=48)
        result = engine.sweep()
        assert len(result.promoted) == 2

    def test_custom_weights(self, home: Path) -> None:
        """Custom signal weights affect scoring."""
        # Weight importance heavily
        engine = PromotionEngine(
            home,
            thresholds=PromotionThresholds(
                importance_weight=1.0,
                access_weight=0.0,
                emotion_weight=0.0,
                age_weight=0.0,
                tag_weight=0.0,
                short_to_mid=0.8,
            ),
        )
        _write_memory(home, "high-imp", importance=0.9, age_hours=48)
        _write_memory(home, "low-imp", importance=0.3, age_hours=48)

        result = engine.sweep()
        promoted_ids = [c.memory_id for c in result.promoted]
        assert "high-imp" in promoted_ids
        assert "low-imp" not in promoted_ids


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestModels:
    """Tests for promotion models."""

    def test_thresholds_defaults(self) -> None:
        """PromotionThresholds has sensible defaults."""
        t = PromotionThresholds()
        assert t.short_to_mid < t.mid_to_long
        assert sum([
            t.access_weight,
            t.importance_weight,
            t.emotion_weight,
            t.age_weight,
            t.tag_weight,
        ]) == pytest.approx(1.0)

    def test_sweep_result_defaults(self) -> None:
        """SweepResult has sensible defaults."""
        r = SweepResult()
        assert r.scanned == 0
        assert r.promoted == []
        assert r.dry_run is False
