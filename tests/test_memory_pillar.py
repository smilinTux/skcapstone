"""Unit tests for the memory pillar module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from skcapstone.models import MemoryLayer, PillarStatus
from skcapstone.pillars.memory import get_memory_stats, initialize_memory


class TestInitializeMemory:
    """Tests for initialize_memory()."""

    def test_creates_memory_directory(self, tmp_agent_home: Path):
        initialize_memory(tmp_agent_home)
        assert (tmp_agent_home / "memory").is_dir()

    def test_creates_layer_subdirectories(self, tmp_agent_home: Path):
        initialize_memory(tmp_agent_home)
        memory_dir = tmp_agent_home / "memory"
        for layer in MemoryLayer:
            assert (memory_dir / layer.value).is_dir(), f"missing layer: {layer.value}"

    def test_creates_short_term_dir(self, tmp_agent_home: Path):
        initialize_memory(tmp_agent_home)
        assert (tmp_agent_home / "memory" / "short-term").is_dir()

    def test_creates_mid_term_dir(self, tmp_agent_home: Path):
        initialize_memory(tmp_agent_home)
        assert (tmp_agent_home / "memory" / "mid-term").is_dir()

    def test_creates_long_term_dir(self, tmp_agent_home: Path):
        initialize_memory(tmp_agent_home)
        assert (tmp_agent_home / "memory" / "long-term").is_dir()

    def test_returns_active_status(self, tmp_agent_home: Path):
        state = initialize_memory(tmp_agent_home)
        assert state.status == PillarStatus.ACTIVE

    def test_store_path_set_to_memory_dir(self, tmp_agent_home: Path):
        state = initialize_memory(tmp_agent_home)
        assert state.store_path == tmp_agent_home / "memory"

    def test_idempotent_second_call(self, tmp_agent_home: Path):
        initialize_memory(tmp_agent_home)
        state2 = initialize_memory(tmp_agent_home)
        assert state2.status == PillarStatus.ACTIVE

    def test_memory_home_param_ignored(self, tmp_agent_home: Path):
        """memory_home is kept for backward compatibility but unused."""
        state = initialize_memory(tmp_agent_home, memory_home=Path("/dev/null"))
        assert state.store_path == tmp_agent_home / "memory"


class TestGetMemoryStats:
    """Tests for get_memory_stats()."""

    def test_returns_zero_counts_when_empty(self, tmp_agent_home: Path):
        initialize_memory(tmp_agent_home)
        stats = get_memory_stats(tmp_agent_home)
        assert stats["short_term"] == 0
        assert stats["mid_term"] == 0
        assert stats["long_term"] == 0
        assert stats["total"] == 0

    def test_counts_json_files_in_short_term(self, tmp_agent_home: Path):
        initialize_memory(tmp_agent_home)
        short_dir = tmp_agent_home / "memory" / "short-term"
        (short_dir / "mem001.json").write_text("{}", encoding="utf-8")
        (short_dir / "mem002.json").write_text("{}", encoding="utf-8")

        stats = get_memory_stats(tmp_agent_home)
        assert stats["short_term"] == 2
        assert stats["total"] == 2

    def test_counts_json_files_in_mid_term(self, tmp_agent_home: Path):
        initialize_memory(tmp_agent_home)
        mid_dir = tmp_agent_home / "memory" / "mid-term"
        (mid_dir / "mem001.json").write_text("{}", encoding="utf-8")

        stats = get_memory_stats(tmp_agent_home)
        assert stats["mid_term"] == 1

    def test_counts_json_files_in_long_term(self, tmp_agent_home: Path):
        initialize_memory(tmp_agent_home)
        long_dir = tmp_agent_home / "memory" / "long-term"
        (long_dir / "deep001.json").write_text("{}", encoding="utf-8")
        (long_dir / "deep002.json").write_text("{}", encoding="utf-8")
        (long_dir / "deep003.json").write_text("{}", encoding="utf-8")

        stats = get_memory_stats(tmp_agent_home)
        assert stats["long_term"] == 3

    def test_total_is_sum_of_all_layers(self, tmp_agent_home: Path):
        initialize_memory(tmp_agent_home)
        (tmp_agent_home / "memory" / "short-term" / "a.json").write_text("{}")
        (tmp_agent_home / "memory" / "mid-term" / "b.json").write_text("{}")
        (tmp_agent_home / "memory" / "long-term" / "c.json").write_text("{}")

        stats = get_memory_stats(tmp_agent_home)
        assert stats["total"] == 3

    def test_non_json_files_not_counted(self, tmp_agent_home: Path):
        initialize_memory(tmp_agent_home)
        short_dir = tmp_agent_home / "memory" / "short-term"
        (short_dir / "note.md").write_text("# note")
        (short_dir / "real.json").write_text("{}")

        stats = get_memory_stats(tmp_agent_home)
        assert stats["short_term"] == 1

    def test_returns_dict_with_expected_keys(self, tmp_agent_home: Path):
        initialize_memory(tmp_agent_home)
        stats = get_memory_stats(tmp_agent_home)
        assert set(stats.keys()) == {"short_term", "mid_term", "long_term", "total"}
