"""Tests for Memory Migration — JSON memories to unified backend."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.migrate_memories import _scan_json_memories, _verify_migration, migrate
from skcapstone.models import MemoryEntry, MemoryLayer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory_json(
    memory_id: str = "test-001",
    content: str = "Test memory content",
    tags: list[str] | None = None,
    source: str = "test",
    layer: str = "short-term",
    importance: float = 0.5,
) -> str:
    """Return a valid MemoryEntry JSON string."""
    return json.dumps(
        {
            "memory_id": memory_id,
            "content": content,
            "tags": tags or ["test"],
            "source": source,
            "layer": layer,
            "importance": importance,
        }
    )


def _populate_layer(home: Path, layer: str, entries: dict[str, str]) -> None:
    """Write JSON files into home/memory/<layer>/."""
    layer_dir = home / "memory" / layer
    layer_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in entries.items():
        (layer_dir / filename).write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# TestScanJsonMemories
# ---------------------------------------------------------------------------


class TestScanJsonMemories:
    """Tests for _scan_json_memories."""

    def test_empty_home_returns_empty_list(self, tmp_path: Path) -> None:
        """An empty home directory yields no memories."""
        result = _scan_json_memories(tmp_path)
        assert result == []

    def test_memories_in_different_layers_are_found(self, tmp_path: Path) -> None:
        """Memories across short-term, mid-term, and long-term are all collected."""
        _populate_layer(
            tmp_path,
            "short-term",
            {"a.json": _make_memory_json(memory_id="st-1", content="short")},
        )
        _populate_layer(
            tmp_path,
            "mid-term",
            {"b.json": _make_memory_json(memory_id="mt-1", content="mid")},
        )
        _populate_layer(
            tmp_path,
            "long-term",
            {"c.json": _make_memory_json(memory_id="lt-1", content="long")},
        )

        result = _scan_json_memories(tmp_path)
        ids = {e.memory_id for e in result}
        assert ids == {"st-1", "mt-1", "lt-1"}
        assert len(result) == 3

    def test_invalid_json_files_are_skipped(self, tmp_path: Path) -> None:
        """Files containing invalid JSON are skipped without raising."""
        _populate_layer(
            tmp_path,
            "short-term",
            {
                "good.json": _make_memory_json(memory_id="good-1"),
                "bad.json": "NOT VALID JSON {{{",
            },
        )

        result = _scan_json_memories(tmp_path)
        assert len(result) == 1
        assert result[0].memory_id == "good-1"

    def test_index_json_is_skipped(self, tmp_path: Path) -> None:
        """index.json files are always skipped."""
        _populate_layer(
            tmp_path,
            "short-term",
            {
                "index.json": json.dumps({"some": "index data"}),
                "real.json": _make_memory_json(memory_id="real-1"),
            },
        )

        result = _scan_json_memories(tmp_path)
        assert len(result) == 1
        assert result[0].memory_id == "real-1"

    def test_missing_layer_dirs_are_handled(self, tmp_path: Path) -> None:
        """If only some layer directories exist, others are silently skipped."""
        # Create only short-term
        _populate_layer(
            tmp_path,
            "short-term",
            {"m.json": _make_memory_json(memory_id="only-1")},
        )
        # mid-term and long-term do not exist

        result = _scan_json_memories(tmp_path)
        assert len(result) == 1
        assert result[0].memory_id == "only-1"

    def test_entries_have_correct_fields(self, tmp_path: Path) -> None:
        """Parsed MemoryEntry objects carry the expected field values."""
        _populate_layer(
            tmp_path,
            "long-term",
            {
                "entry.json": _make_memory_json(
                    memory_id="lt-99",
                    content="important stuff",
                    tags=["alpha", "beta"],
                    source="migration",
                    layer="long-term",
                    importance=0.9,
                )
            },
        )

        result = _scan_json_memories(tmp_path)
        assert len(result) == 1
        entry = result[0]
        assert entry.memory_id == "lt-99"
        assert entry.content == "important stuff"
        assert entry.tags == ["alpha", "beta"]
        assert entry.source == "migration"
        assert entry.layer == MemoryLayer.LONG_TERM
        assert entry.importance == 0.9

    def test_invalid_model_data_is_skipped(self, tmp_path: Path) -> None:
        """JSON that is valid but fails MemoryEntry validation is skipped."""
        # content is required, omitting it should cause a validation error
        bad_model_json = json.dumps({"memory_id": "bad-model"})
        _populate_layer(
            tmp_path,
            "short-term",
            {
                "bad_model.json": bad_model_json,
                "ok.json": _make_memory_json(memory_id="ok-1"),
            },
        )

        result = _scan_json_memories(tmp_path)
        assert len(result) == 1
        assert result[0].memory_id == "ok-1"


# ---------------------------------------------------------------------------
# TestMigrate
# ---------------------------------------------------------------------------


class TestMigrate:
    """Tests for the migrate function."""

    @patch("skcapstone.memory_adapter.get_unified", return_value=None)
    def test_returns_error_when_skmemory_not_available(
        self, mock_get: MagicMock, tmp_path: Path
    ) -> None:
        """Returns an error dict when get_unified returns None."""
        result = migrate(tmp_path)
        assert result["ok"] is False
        assert "skmemory not available" in result["error"]

    @patch("skcapstone.memory_adapter.get_unified")
    def test_dry_run_returns_counts_without_writing(
        self, mock_get: MagicMock, tmp_path: Path
    ) -> None:
        """Dry run scans memories and reports counts but does not call store."""
        mock_store = MagicMock()
        mock_get.return_value = mock_store

        _populate_layer(
            tmp_path,
            "short-term",
            {
                "a.json": _make_memory_json(memory_id="dr-1"),
                "b.json": _make_memory_json(memory_id="dr-2"),
            },
        )

        result = migrate(tmp_path, dry_run=True)
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert result["total_json"] == 2
        assert result["migrated"] == 0
        # No writes should have happened
        mock_store.primary.save.assert_not_called()

    @patch("skcapstone.memory_adapter.entry_to_memory")
    @patch("skcapstone.memory_adapter.get_unified")
    def test_skips_existing_memories(
        self,
        mock_get: MagicMock,
        mock_entry_to_memory: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Memories already present in the store are skipped (deduplication)."""
        mock_store = MagicMock()
        # Simulate existing memory with id "dup-1"
        existing_mem = MagicMock()
        existing_mem.id = "dup-1"
        mock_store.list_memories.return_value = [existing_mem]
        mock_store.vector = None
        mock_store.graph = None
        mock_get.return_value = mock_store

        _populate_layer(
            tmp_path,
            "short-term",
            {"dup.json": _make_memory_json(memory_id="dup-1")},
        )

        result = migrate(tmp_path)
        assert result["ok"] is True
        assert result["skipped_existing"] == 1
        assert result["migrated"] == 0
        mock_entry_to_memory.assert_not_called()

    @patch("skcapstone.memory_adapter.entry_to_memory")
    @patch("skcapstone.memory_adapter.get_unified")
    def test_migrates_new_memories(
        self,
        mock_get: MagicMock,
        mock_entry_to_memory: MagicMock,
        tmp_path: Path,
    ) -> None:
        """New memories are converted and saved to the store."""
        mock_store = MagicMock()
        mock_store.list_memories.return_value = []  # nothing existing
        mock_store.vector = None
        mock_store.graph = None
        mock_get.return_value = mock_store

        mock_memory = MagicMock()
        mock_entry_to_memory.return_value = mock_memory

        _populate_layer(
            tmp_path,
            "short-term",
            {
                "m1.json": _make_memory_json(memory_id="new-1"),
                "m2.json": _make_memory_json(memory_id="new-2"),
            },
        )

        result = migrate(tmp_path)
        assert result["ok"] is True
        assert result["migrated"] == 2
        assert result["skipped_existing"] == 0
        assert mock_store.primary.save.call_count == 2
        assert mock_memory.seal.call_count == 2

    @patch("skcapstone.memory_adapter.entry_to_memory")
    @patch("skcapstone.memory_adapter.get_unified")
    def test_records_errors_for_failed_entries(
        self,
        mock_get: MagicMock,
        mock_entry_to_memory: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Entries that fail during migration are recorded in errors list."""
        mock_store = MagicMock()
        mock_store.list_memories.return_value = []
        mock_store.vector = None
        mock_store.graph = None
        mock_get.return_value = mock_store

        mock_entry_to_memory.side_effect = RuntimeError("conversion failed")

        _populate_layer(
            tmp_path,
            "short-term",
            {"fail.json": _make_memory_json(memory_id="fail-1")},
        )

        result = migrate(tmp_path)
        assert result["ok"] is True  # ok tracks overall process, not individual errors
        assert result["migrated"] == 0
        assert len(result["errors"]) == 1
        assert "fail-1" in result["errors"][0]
        assert "conversion failed" in result["errors"][0]

    @patch("skcapstone.memory_adapter.entry_to_memory")
    @patch("skcapstone.memory_adapter.get_unified")
    def test_vector_and_graph_failures_do_not_block_primary(
        self,
        mock_get: MagicMock,
        mock_entry_to_memory: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Failures in vector/graph backends do not prevent primary save."""
        mock_store = MagicMock()
        mock_store.list_memories.return_value = []
        mock_store.vector = MagicMock()
        mock_store.graph = MagicMock()
        mock_store.vector.save.side_effect = RuntimeError("skvector down")
        mock_store.graph.index_memory.side_effect = RuntimeError("skgraph down")
        mock_get.return_value = mock_store

        mock_memory = MagicMock()
        mock_entry_to_memory.return_value = mock_memory

        _populate_layer(
            tmp_path,
            "short-term",
            {"v.json": _make_memory_json(memory_id="vec-1")},
        )

        result = migrate(tmp_path)
        assert result["migrated"] == 1
        assert len(result["errors"]) == 0  # vector/graph errors are debug-logged, not recorded

    @patch("skcapstone.memory_adapter.get_unified")
    def test_verify_delegates_to_verify_migration(
        self, mock_get: MagicMock, tmp_path: Path
    ) -> None:
        """When verify=True, migrate delegates to _verify_migration."""
        mock_store = MagicMock()
        mock_store.recall.return_value = MagicMock()  # present
        mock_get.return_value = mock_store

        _populate_layer(
            tmp_path,
            "short-term",
            {"v.json": _make_memory_json(memory_id="ver-1")},
        )

        result = migrate(tmp_path, verify=True)
        assert result["verify"] is True
        assert result["ok"] is True
        assert result["verified"] == 1
        assert result["missing"] == []


# ---------------------------------------------------------------------------
# TestVerifyMigration
# ---------------------------------------------------------------------------


class TestVerifyMigration:
    """Tests for _verify_migration."""

    def _make_entries(self, ids: list[str]) -> list[MemoryEntry]:
        """Create minimal MemoryEntry list from a set of ids."""
        return [
            MemoryEntry(memory_id=mid, content=f"Content for {mid}") for mid in ids
        ]

    def test_all_present_returns_ok_true(self) -> None:
        """When all entries are found in the store, ok is True."""
        entries = self._make_entries(["a", "b", "c"])
        mock_store = MagicMock()
        mock_store.recall.return_value = MagicMock()  # always returns something

        result = {
            "ok": True,
            "total_json": 3,
            "migrated": 0,
            "skipped_existing": 0,
            "errors": [],
        }
        out = _verify_migration(entries, mock_store, result)
        assert out["ok"] is True
        assert out["verified"] == 3
        assert out["missing"] == []

    def test_missing_memories_reported(self) -> None:
        """When store.recall returns None, the memory id appears in missing."""
        entries = self._make_entries(["found-1", "gone-1", "gone-2"])
        mock_store = MagicMock()

        def recall_side(mid: str):
            if mid == "found-1":
                return MagicMock()
            return None

        mock_store.recall.side_effect = recall_side

        result = {
            "ok": True,
            "total_json": 3,
            "migrated": 0,
            "skipped_existing": 0,
            "errors": [],
        }
        out = _verify_migration(entries, mock_store, result)
        assert out["ok"] is False
        assert out["verified"] == 1
        assert set(out["missing"]) == {"gone-1", "gone-2"}

    def test_store_recall_exception_handled(self) -> None:
        """When store.recall raises, the memory is treated as missing."""
        entries = self._make_entries(["err-1"])
        mock_store = MagicMock()
        mock_store.recall.side_effect = RuntimeError("db failure")

        result = {
            "ok": True,
            "total_json": 1,
            "migrated": 0,
            "skipped_existing": 0,
            "errors": [],
        }
        out = _verify_migration(entries, mock_store, result)
        assert out["ok"] is False
        assert "err-1" in out["missing"]

    def test_empty_entries_returns_ok(self) -> None:
        """Verifying an empty entry list returns ok with zero counts."""
        mock_store = MagicMock()
        result = {
            "ok": True,
            "total_json": 0,
            "migrated": 0,
            "skipped_existing": 0,
            "errors": [],
        }
        out = _verify_migration([], mock_store, result)
        assert out["ok"] is True
        assert out["verified"] == 0
        assert out["missing"] == []

    def test_verify_flag_is_set(self) -> None:
        """The result dict gets verify=True added by _verify_migration."""
        entries = self._make_entries(["x"])
        mock_store = MagicMock()
        mock_store.recall.return_value = MagicMock()

        result = {
            "ok": True,
            "total_json": 1,
            "migrated": 0,
            "skipped_existing": 0,
            "errors": [],
        }
        out = _verify_migration(entries, mock_store, result)
        assert out["verify"] is True
