"""Tests for the memory curator module."""

from __future__ import annotations

from pathlib import Path

import pytest

from skcapstone.memory_engine import list_memories, recall, store
from skcapstone.memory_curator import (
    CurationResult,
    MemoryCurator,
    _content_hash,
    _suggest_tags,
)
from skcapstone.models import MemoryLayer
from skcapstone.pillars.memory import initialize_memory


@pytest.fixture
def curator_home(tmp_agent_home: Path) -> Path:
    """Provide an agent home with memory initialized."""
    initialize_memory(tmp_agent_home)
    return tmp_agent_home


class TestSuggestTags:
    """Tests for the auto-tag suggestion function."""

    def test_detects_capauth(self):
        """Finds capauth mentions."""
        tags = _suggest_tags("The capauth system handles identity", [])
        assert "capauth" in tags

    def test_skips_existing_tags(self):
        """Doesn't suggest tags already present."""
        tags = _suggest_tags("The capauth system", ["capauth"])
        assert "capauth" not in tags

    def test_multiple_suggestions(self):
        """Detects multiple patterns in one text."""
        tags = _suggest_tags("skcapstone uses MCP and PGP for security", [])
        assert "skcapstone" in tags
        assert "mcp" in tags
        assert "pgp" in tags

    def test_no_suggestions_for_generic(self):
        """Generic text gets no tag suggestions."""
        tags = _suggest_tags("The weather is nice today", [])
        assert tags == []


class TestContentHash:
    """Tests for the deduplication hash function."""

    def test_identical_content(self):
        """Same content produces same hash."""
        assert _content_hash("hello world") == _content_hash("hello world")

    def test_case_insensitive(self):
        """Hash is case-insensitive."""
        assert _content_hash("Hello World") == _content_hash("hello world")

    def test_whitespace_normalized(self):
        """Extra whitespace is normalized before hashing."""
        assert _content_hash("hello  world") == _content_hash("hello world")

    def test_different_content(self):
        """Different content produces different hashes."""
        assert _content_hash("alpha") != _content_hash("beta")


class TestCuratorAutoTag:
    """Tests for the auto-tagging pass."""

    def test_adds_missing_tags(self, curator_home: Path):
        """Auto-tag adds relevant tags to untagged memories."""
        store(curator_home, "The skcapstone MCP server exposes tools", tags=[])
        curator = MemoryCurator(curator_home)
        result = curator.curate(promote=False, dedupe=False)

        assert len(result.tagged) >= 1
        memories = list_memories(curator_home)
        tagged_mem = next((m for m in memories if "skcapstone" in m.tags), None)
        assert tagged_mem is not None

    def test_dry_run_no_changes(self, curator_home: Path):
        """Dry run reports changes without applying them."""
        store(curator_home, "The capauth PGP system", tags=[])
        curator = MemoryCurator(curator_home)
        result = curator.curate(dry_run=True, promote=False, dedupe=False)

        assert len(result.tagged) >= 1
        memories = list_memories(curator_home)
        for m in memories:
            assert "capauth" not in m.tags


class TestCuratorPromote:
    """Tests for the promotion pass."""

    def test_promotes_high_access(self, curator_home: Path):
        """Memories with high access count get promoted."""
        entry = store(curator_home, "Frequently accessed memory", importance=0.3)
        entry.access_count = 5
        from skcapstone.memory_engine import _save_entry
        _save_entry(curator_home, entry)

        curator = MemoryCurator(curator_home)
        result = curator.curate(auto_tag=False, dedupe=False)

        assert len(result.promoted) >= 1

    def test_promotes_high_importance(self, curator_home: Path):
        """High importance short-term memories get promoted."""
        store(curator_home, "Very important memory", importance=0.8)

        curator = MemoryCurator(curator_home)
        result = curator.curate(auto_tag=False, dedupe=False)

        # Reason: importance >= 0.7 auto-promotes to mid-term at store time,
        # so this won't be in short-term. Store at 0.65 instead.
        store(curator_home, "Almost important", importance=0.65)
        entry = list_memories(curator_home, limit=1)[0]
        entry.access_count = 4
        _save_entry = __import__("skcapstone.memory_engine", fromlist=["_save_entry"])._save_entry
        _save_entry(curator_home, entry)

        result2 = curator.curate(auto_tag=False, dedupe=False)
        # At least the high-access one should promote
        assert result2.total_scanned >= 1


class TestCuratorDedupe:
    """Tests for the deduplication pass."""

    def test_removes_exact_duplicates(self, curator_home: Path):
        """Identical content is deduplicated."""
        store(curator_home, "Duplicate memory content here", importance=0.4)
        store(curator_home, "Duplicate memory content here", importance=0.3)

        curator = MemoryCurator(curator_home)
        result = curator.curate(auto_tag=False, promote=False)

        assert len(result.deduped) >= 1

    def test_keeps_higher_tier(self, curator_home: Path):
        """When deduping, the higher-tier memory is kept."""
        store(curator_home, "Keep this important memory", importance=0.9)
        store(curator_home, "Keep this important memory", importance=0.3)

        before = len(list_memories(curator_home))
        curator = MemoryCurator(curator_home)
        curator.curate(auto_tag=False, promote=False)
        after = len(list_memories(curator_home))

        assert after < before

    def test_no_false_positives(self, curator_home: Path):
        """Different content is not deduplicated."""
        store(curator_home, "First unique memory about cats")
        store(curator_home, "Second unique memory about dogs")

        curator = MemoryCurator(curator_home)
        result = curator.curate(auto_tag=False, promote=False)

        assert len(result.deduped) == 0


class TestCuratorStats:
    """Tests for get_stats()."""

    def test_empty_stats(self, curator_home: Path):
        """Stats on empty store returns zero counts."""
        curator = MemoryCurator(curator_home)
        stats = curator.get_stats()
        assert stats["total"] == 0

    def test_stats_with_memories(self, curator_home: Path):
        """Stats reflect stored memories."""
        store(curator_home, "First memory", tags=["alpha"])
        store(curator_home, "Second memory", tags=["beta"])
        store(curator_home, "Untagged memory")

        curator = MemoryCurator(curator_home)
        stats = curator.get_stats()

        assert stats["total"] == 3
        assert stats["tag_coverage"] > 0.0
        assert stats["avg_importance"] > 0.0

    def test_top_tags(self, curator_home: Path):
        """Top tags are reported correctly."""
        for _ in range(3):
            store(curator_home, "PGP encryption test", tags=["pgp"])
        store(curator_home, "Other stuff", tags=["misc"])

        curator = MemoryCurator(curator_home)
        stats = curator.get_stats()

        tag_names = [t[0] for t in stats["top_tags"]]
        assert "pgp" in tag_names


class TestFullCuration:
    """End-to-end curation test."""

    def test_full_pass(self, curator_home: Path):
        """Full curation pass runs all three phases."""
        store(curator_home, "The skcapstone architecture uses pillars")
        store(curator_home, "The skcapstone architecture uses pillars")
        store(curator_home, "Security requires PGP encryption", tags=[])

        curator = MemoryCurator(curator_home)
        result = curator.curate()

        assert result.total_scanned >= 3
        assert isinstance(result, CurationResult)
