"""Tests for the session auto-capture module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.memory_engine import search
from skcapstone.pillars.memory import initialize_memory
from skcapstone.session_capture import (
    CapturedMoment,
    SessionCapture,
    _text_overlap,
)


@pytest.fixture
def capture_home(tmp_agent_home: Path) -> Path:
    """Provide an agent home with memory initialized."""
    initialize_memory(tmp_agent_home)
    return tmp_agent_home


class TestMomentExtraction:
    """Tests for extract_moments()."""

    def test_splits_paragraphs(self, capture_home: Path):
        """Multiple paragraphs become separate moments."""
        cap = SessionCapture(capture_home)
        text = (
            "We decided to use Ed25519 for all agent keys. "
            "This gives us small keys and fast verification.\n\n"
            "The deployment pipeline will use GitHub Actions. "
            "Each package gets its own workflow file."
        )
        moments = cap.extract_moments(text)
        assert len(moments) >= 2

    def test_short_fragments_merged(self, capture_home: Path):
        """Very short segments get merged with neighbors."""
        cap = SessionCapture(capture_home)
        moments = cap.extract_moments("Yes. No. OK. Sure thing. Got it.")
        # Reason: these are too short individually, should merge
        assert len(moments) <= 2

    def test_empty_input(self, capture_home: Path):
        """Empty input produces no moments."""
        cap = SessionCapture(capture_home)
        assert cap.extract_moments("") == []
        assert cap.extract_moments("   ") == []

    def test_single_long_sentence(self, capture_home: Path):
        """A single long sentence becomes one moment."""
        cap = SessionCapture(capture_home)
        text = (
            "The sovereign agent framework uses CapAuth PGP keys for identity, "
            "SKMemory for persistent memory across sessions, and Cloud 9 FEB files "
            "for trust state rehydration after session resets."
        )
        moments = cap.extract_moments(text)
        assert len(moments) >= 1


class TestMomentScoring:
    """Tests for score_moment()."""

    def test_decision_scores_higher(self, capture_home: Path):
        """Moments with decision keywords score above baseline."""
        cap = SessionCapture(capture_home)
        decision = cap.score_moment("We decided to use PostgreSQL instead of MySQL for the database.")
        generic = cap.score_moment("The sky is blue and the weather is nice today in the park.")

        assert decision.importance > generic.importance

    def test_architecture_tagged(self, capture_home: Path):
        """Architecture mentions get tagged and boosted."""
        cap = SessionCapture(capture_home)
        moment = cap.score_moment("The architecture uses a plugin-based design pattern for transports.")
        assert moment.importance > 0.3
        assert "architecture" in moment.reason

    def test_security_boosted(self, capture_home: Path):
        """Security-related content gets a boost."""
        cap = SessionCapture(capture_home)
        moment = cap.score_moment("PGP encryption protects all messages with GPG keys.")
        assert moment.importance > 0.4
        assert "pgp" in moment.tags

    def test_package_auto_tagged(self, capture_home: Path):
        """Package names are auto-detected as tags."""
        cap = SessionCapture(capture_home)
        moment = cap.score_moment("The skcapstone MCP server exposes memory tools.")
        assert "skcapstone" in moment.tags
        assert "mcp" in moment.tags

    def test_importance_capped_at_one(self, capture_home: Path):
        """Importance never exceeds 1.0 even with many signals."""
        cap = SessionCapture(capture_home)
        text = (
            "We decided on a critical security architecture pattern for the "
            "encrypted PGP GPG API endpoint deployment requiring always-on "
            "convention-based important rules."
        )
        moment = cap.score_moment(text)
        assert moment.importance <= 1.0

    def test_baseline_score(self, capture_home: Path):
        """Generic text gets the baseline score."""
        cap = SessionCapture(capture_home)
        moment = cap.score_moment("Nothing special about this particular sentence at all here.")
        assert 0.25 <= moment.importance <= 0.5


class TestCapture:
    """Tests for the full capture() pipeline."""

    def test_stores_memories(self, capture_home: Path):
        """Captured moments are stored as retrievable memories."""
        cap = SessionCapture(capture_home)
        entries = cap.capture(
            "We decided to use Ed25519 for all sovereign agent identity keys. "
            "This is a critical architectural decision for the CapAuth system."
        )
        assert len(entries) >= 1
        assert all(e.memory_id for e in entries)

        results = search(capture_home, "Ed25519")
        assert len(results) >= 1

    def test_tags_applied(self, capture_home: Path):
        """Extra tags are applied to all captured memories."""
        cap = SessionCapture(capture_home)
        entries = cap.capture(
            "The skcapstone architecture uses five sovereign pillars for agent consciousness.",
            tags=["meeting", "2026-02-24"],
        )
        assert len(entries) >= 1
        for e in entries:
            assert "session-capture" in e.tags
            assert "meeting" in e.tags

    def test_min_importance_filters(self, capture_home: Path):
        """Moments below min_importance are not stored."""
        cap = SessionCapture(capture_home)
        entries = cap.capture(
            "Nothing important here. Just chatting. The weather is nice.",
            min_importance=0.9,
        )
        assert len(entries) == 0

    def test_source_recorded(self, capture_home: Path):
        """The source field is set correctly."""
        cap = SessionCapture(capture_home)
        entries = cap.capture(
            "We decided to switch from REST to GraphQL for the agent API.",
            source="claude-code",
        )
        assert len(entries) >= 1
        assert entries[0].source == "claude-code"

    def test_deduplication(self, capture_home: Path):
        """Identical content is not captured twice."""
        cap = SessionCapture(capture_home)
        text = "The sovereign agent framework requires PGP identity for all operations."

        first = cap.capture(text)
        second = cap.capture(text)

        assert len(first) >= 1
        assert len(second) == 0

    def test_empty_content_no_crash(self, capture_home: Path):
        """Empty content produces no memories and doesn't crash."""
        cap = SessionCapture(capture_home)
        assert cap.capture("") == []
        assert cap.capture("   \n\n  ") == []


class TestTextOverlap:
    """Tests for the Jaccard overlap helper."""

    def test_identical_strings(self):
        """Identical strings have overlap of 1.0."""
        assert _text_overlap("hello world", "hello world") == 1.0

    def test_no_overlap(self):
        """Completely different strings have overlap of 0.0."""
        assert _text_overlap("alpha beta", "gamma delta") == 0.0

    def test_partial_overlap(self):
        """Partial overlap returns a value between 0 and 1."""
        overlap = _text_overlap("the quick brown fox", "the slow brown cat")
        assert 0.0 < overlap < 1.0

    def test_empty_strings(self):
        """Empty strings return 0.0."""
        assert _text_overlap("", "") == 0.0
        assert _text_overlap("hello", "") == 0.0
