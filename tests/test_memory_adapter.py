"""Tests for the Memory Adapter — bridge between skcapstone and skmemory."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.memory_adapter import (
    _LAYER_FROM_SKMEMORY,
    _LAYER_TO_SKMEMORY,
    entry_to_memory,
    get_unified,
    memory_to_entry,
    verify_sync,
    reindex_all,
)
from skcapstone.models import MemoryEntry, MemoryLayer


# ---------------------------------------------------------------------------
# Layer mapping
# ---------------------------------------------------------------------------


class TestLayerMapping:
    """Tests for layer name conversion."""

    def test_to_skmemory_mapping(self) -> None:
        """skcapstone layers map to skmemory strings."""
        assert _LAYER_TO_SKMEMORY[MemoryLayer.SHORT_TERM] == "short-term"
        assert _LAYER_TO_SKMEMORY[MemoryLayer.MID_TERM] == "mid-term"
        assert _LAYER_TO_SKMEMORY[MemoryLayer.LONG_TERM] == "long-term"

    def test_from_skmemory_mapping(self) -> None:
        """skmemory strings map back to skcapstone layers."""
        assert _LAYER_FROM_SKMEMORY["short-term"] == MemoryLayer.SHORT_TERM
        assert _LAYER_FROM_SKMEMORY["mid-term"] == MemoryLayer.MID_TERM
        assert _LAYER_FROM_SKMEMORY["long-term"] == MemoryLayer.LONG_TERM

    def test_roundtrip(self) -> None:
        """Layer mappings are invertible."""
        for layer in MemoryLayer:
            sk_str = _LAYER_TO_SKMEMORY[layer]
            assert _LAYER_FROM_SKMEMORY[sk_str] == layer


# ---------------------------------------------------------------------------
# entry_to_memory conversion
# ---------------------------------------------------------------------------


class TestEntryToMemory:
    """Tests for converting MemoryEntry to skmemory Memory."""

    def test_basic_conversion(self) -> None:
        """Basic entry converts to Memory with correct fields."""
        entry = MemoryEntry(
            memory_id="abc123",
            content="Test memory content",
            tags=["test", "unit"],
            source="cli",
            layer=MemoryLayer.SHORT_TERM,
            importance=0.7,
        )
        mem = entry_to_memory(entry)
        assert mem.id == "abc123"
        assert mem.content == "Test memory content"
        assert mem.tags == ["test", "unit"]
        assert mem.source == "cli"
        assert mem.layer.value == "short-term"

    def test_title_from_content(self) -> None:
        """Title is derived from first 80 chars of content."""
        entry = MemoryEntry(
            memory_id="abc",
            content="A" * 100,
        )
        mem = entry_to_memory(entry)
        assert len(mem.title) == 80

    def test_emotional_intensity_from_importance(self) -> None:
        """Importance maps to emotional intensity (x10)."""
        entry = MemoryEntry(
            memory_id="abc",
            content="Test",
            importance=0.8,
        )
        mem = entry_to_memory(entry)
        assert mem.emotional.intensity == 8.0

    def test_metadata_preserved(self) -> None:
        """Extra metadata fields are preserved."""
        entry = MemoryEntry(
            memory_id="abc",
            content="Test",
            metadata={"custom_key": "custom_value"},
            access_count=5,
            soul_context="lumina",
        )
        mem = entry_to_memory(entry)
        assert mem.metadata["access_count"] == 5
        assert mem.metadata["soul_context"] == "lumina"
        assert mem.metadata["custom_key"] == "custom_value"

    def test_all_layers(self) -> None:
        """All layer types convert correctly."""
        for layer in MemoryLayer:
            entry = MemoryEntry(
                memory_id=f"id-{layer.value}",
                content="Test",
                layer=layer,
            )
            mem = entry_to_memory(entry)
            assert mem.layer.value == _LAYER_TO_SKMEMORY[layer]


# ---------------------------------------------------------------------------
# memory_to_entry conversion
# ---------------------------------------------------------------------------


class TestMemoryToEntry:
    """Tests for converting skmemory Memory to MemoryEntry."""

    def test_roundtrip(self) -> None:
        """Entry → Memory → Entry preserves key fields."""
        original = MemoryEntry(
            memory_id="round-trip",
            content="Round trip test",
            tags=["test"],
            source="mcp",
            layer=MemoryLayer.MID_TERM,
            importance=0.6,
            access_count=3,
        )
        mem = entry_to_memory(original)
        restored = memory_to_entry(mem)
        assert restored.memory_id == original.memory_id
        assert restored.content == original.content
        assert restored.tags == original.tags
        assert restored.source == original.source
        assert restored.layer == original.layer
        assert abs(restored.importance - original.importance) < 0.01
        assert restored.access_count == original.access_count

    def test_importance_clamped(self) -> None:
        """Importance is clamped to [0.0, 1.0]."""
        from skmemory.models import EmotionalSnapshot, Memory, MemoryLayer as SKLayer

        mem = Memory(
            id="test",
            title="test",
            content="test",
            layer=SKLayer("short-term"),
            tags=[],
            source="test",
            emotional=EmotionalSnapshot(intensity=10.0),  # 10 / 10 = 1.0
            metadata={},
        )
        entry = memory_to_entry(mem)
        assert entry.importance <= 1.0
        assert entry.importance >= 0.0

    def test_soul_context_extracted(self) -> None:
        """soul_context is extracted from metadata."""
        original = MemoryEntry(
            memory_id="soul-test",
            content="Soul context test",
            soul_context="opus",
        )
        mem = entry_to_memory(original)
        restored = memory_to_entry(mem)
        assert restored.soul_context == "opus"


# ---------------------------------------------------------------------------
# get_unified singleton
# ---------------------------------------------------------------------------


class TestGetUnified:
    """Tests for the unified store singleton."""

    def test_returns_none_without_skmemory(self) -> None:
        """Returns None when skmemory is not available."""
        import skcapstone.memory_adapter as ma

        old_checked = ma._unified_checked
        old_store = ma._unified_store
        try:
            ma._unified_checked = False
            ma._unified_store = None
            with patch.object(ma, "_skmemory_available", return_value=False):
                with patch.object(ma, "_get_store", return_value=None):
                    result = get_unified()
            assert result is None
        finally:
            ma._unified_checked = old_checked
            ma._unified_store = old_store

    def test_caches_result(self) -> None:
        """Second call returns cached store, doesn't recreate."""
        import skcapstone.memory_adapter as ma

        old_checked = ma._unified_checked
        old_store = ma._unified_store
        try:
            ma._unified_checked = False
            ma._unified_store = None
            mock_store = MagicMock()
            with patch.object(ma, "_get_store", return_value=mock_store) as mock_get:
                first = get_unified()
                second = get_unified()
            assert first is second
            assert mock_get.call_count == 1
        finally:
            ma._unified_checked = old_checked
            ma._unified_store = old_store


# ---------------------------------------------------------------------------
# verify_sync
# ---------------------------------------------------------------------------


class TestVerifySync:
    """Tests for sync verification."""

    def test_returns_not_available_without_skmemory(self) -> None:
        """Returns error dict when no store available."""
        import skcapstone.memory_adapter as ma

        old_checked = ma._unified_checked
        old_store = ma._unified_store
        try:
            ma._unified_checked = True
            ma._unified_store = None
            result = verify_sync()
            assert result["synced"] is False
            assert "not available" in result["reason"]
        finally:
            ma._unified_checked = old_checked
            ma._unified_store = old_store

    def test_detects_sync(self) -> None:
        """Reports synced when counts match."""
        mock_store = MagicMock()
        mock_store.health.return_value = {
            "primary": {"ok": True},
            "vector": {"ok": True, "point_count": 10},
        }
        mock_store.primary.stats.return_value = {"total": 10}

        import skcapstone.memory_adapter as ma

        old_checked = ma._unified_checked
        old_store = ma._unified_store
        try:
            ma._unified_checked = True
            ma._unified_store = mock_store
            result = verify_sync()
            assert result["synced"] is True
        finally:
            ma._unified_checked = old_checked
            ma._unified_store = old_store

    def test_detects_mismatch(self) -> None:
        """Reports not synced when counts differ."""
        mock_store = MagicMock()
        mock_store.health.return_value = {
            "primary": {"ok": True},
            "vector": {"ok": True, "point_count": 5},
        }
        mock_store.primary.stats.return_value = {"total": 10}

        import skcapstone.memory_adapter as ma

        old_checked = ma._unified_checked
        old_store = ma._unified_store
        try:
            ma._unified_checked = True
            ma._unified_store = mock_store
            result = verify_sync()
            assert result["synced"] is False
            assert "mismatch" in result.get("reason", "").lower()
        finally:
            ma._unified_checked = old_checked
            ma._unified_store = old_store


# ---------------------------------------------------------------------------
# reindex_all
# ---------------------------------------------------------------------------


class TestReindexAll:
    """Tests for full reindex."""

    def test_returns_not_available_without_store(self) -> None:
        """Returns error when no store available."""
        import skcapstone.memory_adapter as ma

        old_checked = ma._unified_checked
        old_store = ma._unified_store
        try:
            ma._unified_checked = True
            ma._unified_store = None
            result = reindex_all()
            assert result["ok"] is False
        finally:
            ma._unified_checked = old_checked
            ma._unified_store = old_store

    def test_reindexes_all_memories(self) -> None:
        """Reindex processes all memories from primary."""
        mock_mem = MagicMock()
        mock_mem.id = "test-1"
        mock_store = MagicMock()
        mock_store.list_memories.return_value = [mock_mem, mock_mem]
        mock_store.vector = MagicMock()
        mock_store.graph = MagicMock()

        import skcapstone.memory_adapter as ma

        old_checked = ma._unified_checked
        old_store = ma._unified_store
        try:
            ma._unified_checked = True
            ma._unified_store = mock_store
            result = reindex_all()
            assert result["ok"] is True
            assert result["total"] == 2
            assert result["vector_indexed"] == 2
            assert result["graph_indexed"] == 2
        finally:
            ma._unified_checked = old_checked
            ma._unified_store = old_store
