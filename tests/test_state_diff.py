"""Tests for the state diff module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from skcapstone.coordination import Board, Task
from skcapstone.memory_engine import store
from skcapstone.pillars.identity import generate_identity
from skcapstone.pillars.memory import initialize_memory
from skcapstone.pillars.security import initialize_security
from skcapstone.pillars.sync import initialize_sync
from skcapstone.pillars.trust import initialize_trust, record_trust_state
from skcapstone.state_diff import (
    FORMATTERS,
    StateDiff,
    compute_diff,
    format_json,
    format_text,
    load_snapshot,
    save_snapshot,
    take_snapshot,
)


def _init_agent(home: Path, name: str = "diff-test") -> None:
    """Set up a full agent for testing."""
    generate_identity(home, name)
    initialize_memory(home)
    initialize_trust(home)
    initialize_security(home)
    initialize_sync(home)
    manifest = {"name": name, "version": "0.1.0", "created_at": "2026-01-01T00:00:00Z", "connectors": []}
    (home / "manifest.json").write_text(json.dumps(manifest))
    (home / "config").mkdir(exist_ok=True)
    (home / "config" / "config.yaml").write_text(yaml.dump({"agent_name": name}))


class TestTakeSnapshot:
    """Tests for take_snapshot()."""

    def test_snapshot_structure(self, tmp_agent_home: Path):
        """Snapshot has expected top-level keys."""
        _init_agent(tmp_agent_home)
        snap = take_snapshot(tmp_agent_home)
        assert "timestamp" in snap
        assert "memories" in snap
        assert "trust" in snap
        assert "tasks" in snap
        assert "pillars" in snap

    def test_snapshot_captures_memories(self, tmp_agent_home: Path):
        """Snapshot includes current memories."""
        _init_agent(tmp_agent_home)
        store(tmp_agent_home, "Snapshot test memory")
        snap = take_snapshot(tmp_agent_home)
        assert snap["memories"]["count"] >= 1


class TestSaveLoadSnapshot:
    """Tests for save/load cycle."""

    def test_roundtrip(self, tmp_agent_home: Path):
        """Saved snapshot can be loaded back."""
        _init_agent(tmp_agent_home)
        save_snapshot(tmp_agent_home)
        loaded = load_snapshot(tmp_agent_home)
        assert loaded is not None
        assert "timestamp" in loaded

    def test_load_returns_none_when_empty(self, tmp_agent_home: Path):
        """Load returns None when no snapshot exists."""
        result = load_snapshot(tmp_agent_home)
        assert result is None


class TestComputeDiff:
    """Tests for compute_diff()."""

    def test_no_baseline_shows_all_as_new(self, tmp_agent_home: Path):
        """Without a baseline, everything is new."""
        _init_agent(tmp_agent_home)
        store(tmp_agent_home, "New memory")
        diff = compute_diff(tmp_agent_home)
        assert diff.has_changes
        assert diff.memory_count_now >= 1

    def test_no_changes_after_save(self, tmp_agent_home: Path):
        """Immediately after saving, diff shows no changes."""
        _init_agent(tmp_agent_home)
        store(tmp_agent_home, "Existing memory")
        save_snapshot(tmp_agent_home)
        diff = compute_diff(tmp_agent_home)
        assert not diff.has_changes

    def test_new_memory_detected(self, tmp_agent_home: Path):
        """A memory added after snapshot shows in diff."""
        _init_agent(tmp_agent_home)
        save_snapshot(tmp_agent_home)
        store(tmp_agent_home, "Memory added after snapshot")
        diff = compute_diff(tmp_agent_home)
        assert diff.has_changes
        assert len(diff.new_memories) >= 1

    def test_completed_task_detected(self, tmp_agent_home: Path):
        """A task completed after snapshot shows in diff."""
        _init_agent(tmp_agent_home)
        board = Board(tmp_agent_home)
        board.ensure_dirs()
        task = Task(title="Diff test task")
        board.create_task(task)
        save_snapshot(tmp_agent_home)
        board.claim_task("tester", task.id)
        board.complete_task("tester", task.id)
        diff = compute_diff(tmp_agent_home)
        assert diff.has_changes
        assert len(diff.completed_tasks) >= 1

    def test_trust_change_detected(self, tmp_agent_home: Path):
        """Trust state changes show in diff."""
        _init_agent(tmp_agent_home)
        record_trust_state(tmp_agent_home, depth=5.0, trust_level=0.5, love_intensity=0.5)
        save_snapshot(tmp_agent_home)
        record_trust_state(tmp_agent_home, depth=9.0, trust_level=0.95, love_intensity=0.9, entangled=True)
        diff = compute_diff(tmp_agent_home)
        assert diff.has_changes
        assert "depth" in diff.trust_changes or "trust_level" in diff.trust_changes


class TestFormatText:
    """Tests for text formatter."""

    def test_no_changes_message(self, tmp_agent_home: Path):
        """No-change diff says so."""
        _init_agent(tmp_agent_home)
        save_snapshot(tmp_agent_home)
        diff = compute_diff(tmp_agent_home)
        text = format_text(diff)
        assert "No changes" in text

    def test_new_memories_shown(self, tmp_agent_home: Path):
        """New memories appear in text output."""
        _init_agent(tmp_agent_home)
        save_snapshot(tmp_agent_home)
        store(tmp_agent_home, "Brand new memory for diff")
        diff = compute_diff(tmp_agent_home)
        text = format_text(diff)
        assert "new memor" in text


class TestFormatJson:
    """Tests for JSON formatter."""

    def test_valid_json(self, tmp_agent_home: Path):
        """JSON output is parseable."""
        _init_agent(tmp_agent_home)
        diff = compute_diff(tmp_agent_home)
        output = format_json(diff)
        parsed = json.loads(output)
        assert "has_changes" in parsed
        assert "memories" in parsed


class TestFormattersRegistry:
    """Tests for FORMATTERS dict."""

    def test_both_registered(self):
        assert "text" in FORMATTERS
        assert "json" in FORMATTERS
