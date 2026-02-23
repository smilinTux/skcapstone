"""Tests for the sovereign memory engine."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skcapstone.memory_engine import (
    delete,
    export_for_seed,
    gc_expired,
    get_stats,
    import_from_seed,
    list_memories,
    recall,
    search,
    store,
)
from skcapstone.models import MemoryEntry, MemoryLayer


@pytest.fixture
def agent_home(tmp_path: Path) -> Path:
    """Create a temporary agent home with memory directories."""
    home = tmp_path / ".skcapstone"
    home.mkdir()
    return home


class TestStore:
    """Tests for storing memories."""

    def test_store_basic(self, agent_home: Path):
        """Store a simple memory and verify it persists."""
        entry = store(agent_home, "The capital of France is Paris")
        assert entry.memory_id
        assert entry.content == "The capital of France is Paris"
        assert entry.layer == MemoryLayer.SHORT_TERM
        assert entry.importance == 0.5

        path = agent_home / "memory" / "short-term" / f"{entry.memory_id}.json"
        assert path.exists()

    def test_store_with_tags(self, agent_home: Path):
        """Memories should be storable with tags."""
        entry = store(agent_home, "SKCapstone uses PGP", tags=["skcapstone", "pgp"])
        assert entry.tags == ["skcapstone", "pgp"]

    def test_store_high_importance_promotes(self, agent_home: Path):
        """High-importance memories should auto-promote to mid-term."""
        entry = store(agent_home, "Critical architecture decision", importance=0.8)
        assert entry.layer == MemoryLayer.MID_TERM

        path = agent_home / "memory" / "mid-term" / f"{entry.memory_id}.json"
        assert path.exists()

    def test_store_forced_layer(self, agent_home: Path):
        """Explicit layer should override auto-promotion."""
        entry = store(
            agent_home,
            "Permanent knowledge",
            layer=MemoryLayer.LONG_TERM,
        )
        assert entry.layer == MemoryLayer.LONG_TERM

    def test_store_clamps_importance(self, agent_home: Path):
        """Importance should be clamped to [0.0, 1.0]."""
        entry = store(agent_home, "Over the top", importance=5.0)
        assert entry.importance == 1.0

        entry2 = store(agent_home, "Below zero", importance=-1.0)
        assert entry2.importance == 0.0

    def test_store_creates_directories(self, agent_home: Path):
        """Storing a memory should create layer dirs if missing."""
        store(agent_home, "First memory")
        for layer in MemoryLayer:
            assert (agent_home / "memory" / layer.value).exists()


class TestRecall:
    """Tests for recalling memories."""

    def test_recall_existing(self, agent_home: Path):
        """Recalling a memory should return it and increment access count."""
        original = store(agent_home, "Remember this")
        recalled = recall(agent_home, original.memory_id)

        assert recalled is not None
        assert recalled.content == "Remember this"
        assert recalled.access_count == 1
        assert recalled.accessed_at is not None

    def test_recall_nonexistent(self, agent_home: Path):
        """Recalling a nonexistent memory should return None."""
        result = recall(agent_home, "nonexistent123")
        assert result is None

    def test_recall_promotes_after_threshold(self, agent_home: Path):
        """Memory should promote from short-term to mid-term after 3 accesses."""
        entry = store(agent_home, "Frequently accessed")
        mid = entry.memory_id

        for _ in range(3):
            entry = recall(agent_home, mid)

        assert entry is not None
        assert entry.layer == MemoryLayer.MID_TERM

    def test_recall_promotes_mid_to_long(self, agent_home: Path):
        """Memory should promote from mid-term to long-term after 10 accesses."""
        entry = store(agent_home, "Very important", importance=0.8)
        assert entry.layer == MemoryLayer.MID_TERM

        for _ in range(10):
            entry = recall(agent_home, entry.memory_id)

        assert entry is not None
        assert entry.layer == MemoryLayer.LONG_TERM


class TestSearch:
    """Tests for searching memories."""

    def test_search_by_content(self, agent_home: Path):
        """Search should find memories by content substring."""
        store(agent_home, "Python is a programming language")
        store(agent_home, "Rust is also a language")
        store(agent_home, "Coffee is a drink")

        results = search(agent_home, "language")
        assert len(results) == 2

    def test_search_case_insensitive(self, agent_home: Path):
        """Search should be case-insensitive."""
        store(agent_home, "SKCapstone is SOVEREIGN")
        results = search(agent_home, "sovereign")
        assert len(results) == 1

    def test_search_by_tag(self, agent_home: Path):
        """Search should match tags too."""
        store(agent_home, "Some content", tags=["architecture", "design"])
        store(agent_home, "Other content", tags=["random"])

        results = search(agent_home, "architecture")
        assert len(results) == 1

    def test_search_filter_by_tags(self, agent_home: Path):
        """Tag filter should require ALL specified tags."""
        store(agent_home, "Tagged memory", tags=["python", "ai"])
        store(agent_home, "Tagged memory too", tags=["python"])

        results = search(agent_home, "Tagged", tags=["python", "ai"])
        assert len(results) == 1

    def test_search_filter_by_layer(self, agent_home: Path):
        """Search should respect layer filter."""
        store(agent_home, "Short memory", layer=MemoryLayer.SHORT_TERM)
        store(agent_home, "Long memory", layer=MemoryLayer.LONG_TERM)

        results = search(agent_home, "memory", layer=MemoryLayer.LONG_TERM)
        assert len(results) == 1
        assert results[0].layer == MemoryLayer.LONG_TERM

    def test_search_no_results(self, agent_home: Path):
        """Search with no matches should return empty list."""
        store(agent_home, "Hello world")
        results = search(agent_home, "xyzzy")
        assert results == []

    def test_search_ranks_by_importance(self, agent_home: Path):
        """Higher importance should rank higher."""
        store(agent_home, "Low importance match", importance=0.1)
        store(agent_home, "High importance match", importance=0.6)

        results = search(agent_home, "importance match")
        assert len(results) == 2
        assert results[0].importance > results[1].importance


class TestListMemories:
    """Tests for listing memories."""

    def test_list_all(self, agent_home: Path):
        """List should return all memories."""
        store(agent_home, "One")
        store(agent_home, "Two")
        store(agent_home, "Three")

        entries = list_memories(agent_home)
        assert len(entries) == 3

    def test_list_by_layer(self, agent_home: Path):
        """List should filter by layer."""
        store(agent_home, "Short", layer=MemoryLayer.SHORT_TERM)
        store(agent_home, "Long", layer=MemoryLayer.LONG_TERM)

        short_entries = list_memories(agent_home, layer=MemoryLayer.SHORT_TERM)
        assert len(short_entries) == 1
        assert short_entries[0].content == "Short"

    def test_list_respects_limit(self, agent_home: Path):
        """List should respect the limit parameter."""
        for i in range(10):
            store(agent_home, f"Memory {i}")

        entries = list_memories(agent_home, limit=3)
        assert len(entries) == 3

    def test_list_newest_first(self, agent_home: Path):
        """List should return newest memories first."""
        e1 = store(agent_home, "First")
        e2 = store(agent_home, "Second")

        entries = list_memories(agent_home)
        assert entries[0].memory_id == e2.memory_id


class TestDelete:
    """Tests for deleting memories."""

    def test_delete_existing(self, agent_home: Path):
        """Deleting an existing memory should remove it."""
        entry = store(agent_home, "To be deleted")
        assert delete(agent_home, entry.memory_id)

        path = agent_home / "memory" / "short-term" / f"{entry.memory_id}.json"
        assert not path.exists()

    def test_delete_nonexistent(self, agent_home: Path):
        """Deleting a nonexistent memory should return False."""
        assert not delete(agent_home, "nonexistent123")


class TestStats:
    """Tests for memory statistics."""

    def test_stats_empty(self, agent_home: Path):
        """Empty memory should report zero counts."""
        stats = get_stats(agent_home)
        assert stats.total_memories == 0
        assert stats.short_term == 0

    def test_stats_counts(self, agent_home: Path):
        """Stats should count memories per layer."""
        store(agent_home, "Short 1")
        store(agent_home, "Short 2")
        store(agent_home, "Long 1", layer=MemoryLayer.LONG_TERM)

        stats = get_stats(agent_home)
        assert stats.total_memories == 3
        assert stats.short_term == 2
        assert stats.long_term == 1


class TestGarbageCollection:
    """Tests for expired memory cleanup."""

    def test_gc_removes_old_unaccessed(self, agent_home: Path):
        """GC should remove old short-term memories with zero access."""
        entry = store(agent_home, "Old memory")

        path = agent_home / "memory" / "short-term" / f"{entry.memory_id}.json"
        data = json.loads(path.read_text())
        old_time = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        data["created_at"] = old_time
        path.write_text(json.dumps(data, indent=2))

        removed = gc_expired(agent_home)
        assert removed == 1
        assert not path.exists()

    def test_gc_keeps_accessed_memories(self, agent_home: Path):
        """GC should keep memories that have been accessed."""
        entry = store(agent_home, "Accessed memory")
        recall(agent_home, entry.memory_id)

        path = agent_home / "memory" / "short-term" / f"{entry.memory_id}.json"
        data = json.loads(path.read_text())
        old_time = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        data["created_at"] = old_time
        path.write_text(json.dumps(data, indent=2))

        removed = gc_expired(agent_home)
        assert removed == 0

    def test_gc_keeps_recent_memories(self, agent_home: Path):
        """GC should not remove recent memories."""
        store(agent_home, "Fresh memory")
        removed = gc_expired(agent_home)
        assert removed == 0


class TestSeedIntegration:
    """Tests for seed export/import."""

    def test_export_for_seed(self, agent_home: Path):
        """Export should return memory dicts suitable for seeds."""
        store(agent_home, "Important fact", tags=["fact"], importance=0.9)
        store(agent_home, "Trivial note", importance=0.2)

        exported = export_for_seed(agent_home)
        assert len(exported) == 2
        assert exported[0]["importance"] >= exported[1]["importance"]
        assert "content" in exported[0]
        assert "tags" in exported[0]

    def test_import_from_seed(self, agent_home: Path):
        """Import should create new memories from seed data."""
        seed_data = [
            {
                "memory_id": "foreign001",
                "content": "Imported knowledge",
                "tags": ["imported"],
                "layer": "long-term",
                "importance": 0.8,
                "source": "peer-agent",
            },
            {
                "memory_id": "foreign002",
                "content": "Another import",
                "tags": [],
                "layer": "short-term",
                "importance": 0.3,
                "source": "peer-agent",
            },
        ]

        imported = import_from_seed(agent_home, seed_data)
        assert imported == 2

        entries = list_memories(agent_home)
        assert len(entries) == 2

    def test_import_skips_duplicates(self, agent_home: Path):
        """Import should not duplicate existing memories."""
        entry = store(agent_home, "Already here")

        seed_data = [
            {
                "memory_id": entry.memory_id,
                "content": "Already here",
                "tags": [],
                "layer": "short-term",
                "importance": 0.5,
                "source": "seed",
            },
        ]

        imported = import_from_seed(agent_home, seed_data)
        assert imported == 0

    def test_export_respects_limit(self, agent_home: Path):
        """Export should respect max_entries."""
        for i in range(10):
            store(agent_home, f"Memory {i}")

        exported = export_for_seed(agent_home, max_entries=3)
        assert len(exported) == 3


class TestMemoryEntryModel:
    """Tests for MemoryEntry model properties."""

    def test_should_promote_short_term(self):
        """Short-term memory should promote after 3 accesses."""
        entry = MemoryEntry(content="test", access_count=3)
        assert entry.should_promote

    def test_should_promote_high_importance(self):
        """Short-term memory should promote at importance >= 0.7."""
        entry = MemoryEntry(content="test", importance=0.7)
        assert entry.should_promote

    def test_should_not_promote_new(self):
        """New short-term memory should not promote."""
        entry = MemoryEntry(content="test")
        assert not entry.should_promote

    def test_long_term_never_promotes(self):
        """Long-term is the highest tier."""
        entry = MemoryEntry(
            content="test",
            layer=MemoryLayer.LONG_TERM,
            access_count=100,
            importance=1.0,
        )
        assert not entry.should_promote
