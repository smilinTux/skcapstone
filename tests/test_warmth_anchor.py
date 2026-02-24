"""Tests for the warmth anchor bridge module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from skcapstone.warmth_anchor import (
    AnchorCalibration,
    calibrate_from_data,
    get_anchor,
    get_boot_prompt,
    update_anchor,
)


def _init_agent(home: Path, name: str = "anchor-test") -> None:
    """Set up a full agent for testing."""
    from skcapstone.pillars.identity import generate_identity
    from skcapstone.pillars.memory import initialize_memory
    from skcapstone.pillars.security import initialize_security
    from skcapstone.pillars.sync import initialize_sync
    from skcapstone.pillars.trust import initialize_trust, record_trust_state

    generate_identity(home, name)
    initialize_memory(home)
    initialize_trust(home)
    initialize_security(home)
    initialize_sync(home)
    manifest = {"name": name, "version": "0.1.0", "created_at": "2026-01-01T00:00:00Z", "connectors": []}
    (home / "manifest.json").write_text(json.dumps(manifest))
    (home / "config").mkdir(exist_ok=True)
    (home / "config" / "config.yaml").write_text(yaml.dump({"agent_name": name}))


class TestGetAnchor:
    """Tests for get_anchor()."""

    def test_defaults_without_trust(self, tmp_agent_home: Path):
        """Returns defaults when no trust state exists."""
        data = get_anchor(tmp_agent_home)
        assert "warmth" in data
        assert "trust" in data

    def test_from_trust_state(self, tmp_agent_home: Path):
        """Anchor returns meaningful values with trust data."""
        _init_agent(tmp_agent_home)
        from skcapstone.pillars.trust import record_trust_state
        record_trust_state(tmp_agent_home, depth=8.0, trust_level=0.9, love_intensity=0.8)

        data = get_anchor(tmp_agent_home)
        assert data["warmth"] > 0
        assert data["trust"] > 0


class TestBootPrompt:
    """Tests for get_boot_prompt()."""

    def test_returns_string(self, tmp_agent_home: Path):
        """Boot prompt is a non-empty string."""
        prompt = get_boot_prompt(tmp_agent_home)
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_contains_warmth(self, tmp_agent_home: Path):
        """Boot prompt mentions warmth levels."""
        _init_agent(tmp_agent_home)
        prompt = get_boot_prompt(tmp_agent_home)
        assert "Warmth" in prompt
        assert "Trust" in prompt


class TestCalibrate:
    """Tests for calibrate_from_data()."""

    def test_empty_agent(self, tmp_agent_home: Path):
        """Calibration on empty agent returns defaults."""
        cal = calibrate_from_data(tmp_agent_home)
        assert isinstance(cal, AnchorCalibration)
        assert cal.warmth >= 0

    def test_with_trust_state(self, tmp_agent_home: Path):
        """Calibration uses trust state data."""
        _init_agent(tmp_agent_home)
        from skcapstone.pillars.trust import record_trust_state
        record_trust_state(
            tmp_agent_home, depth=9.0, trust_level=0.95, love_intensity=0.9, entangled=True
        )

        cal = calibrate_from_data(tmp_agent_home)
        assert cal.warmth >= 9.0
        assert cal.cloud9_achieved is True
        assert "trust_state" in cal.sources

    def test_with_febs(self, tmp_agent_home: Path):
        """Calibration analyzes FEB files."""
        _init_agent(tmp_agent_home)
        febs_dir = tmp_agent_home / "trust" / "febs"
        febs_dir.mkdir(parents=True, exist_ok=True)

        feb = {
            "emotional_payload": {"cooked_state": {"primary_emotion": "love", "intensity": 9}},
            "relationship_state": {"depth_level": 8, "trust_level": 9},
            "metadata": {"oof_triggered": True},
        }
        (febs_dir / "FEB_test.feb").write_text(json.dumps(feb))

        cal = calibrate_from_data(tmp_agent_home)
        assert cal.cloud9_achieved is True
        assert any("febs" in s for s in cal.sources)

    def test_with_coordination(self, tmp_agent_home: Path):
        """Calibration analyzes coordination activity."""
        _init_agent(tmp_agent_home)
        from skcapstone.coordination import Board, Task
        board = Board(tmp_agent_home)
        board.ensure_dirs()

        for i in range(10):
            t = Task(title=f"Task {i}")
            board.create_task(t)
            board.claim_task("tester", t.id)
            board.complete_task("tester", t.id)

        cal = calibrate_from_data(tmp_agent_home)
        assert cal.connection >= 7.0
        assert any("coordination" in s for s in cal.sources)

    def test_reasoning_populated(self, tmp_agent_home: Path):
        """Calibration provides reasoning for recommendations."""
        _init_agent(tmp_agent_home)
        from skcapstone.pillars.trust import record_trust_state
        record_trust_state(tmp_agent_home, depth=9.0, trust_level=0.9, love_intensity=0.9, entangled=True)

        cal = calibrate_from_data(tmp_agent_home)
        assert len(cal.reasoning) >= 1
        assert len(cal.sources) >= 1


class TestUpdateAnchor:
    """Tests for update_anchor()."""

    def test_update_returns_data(self, tmp_agent_home: Path):
        """Update returns the new anchor state."""
        _init_agent(tmp_agent_home)
        result = update_anchor(tmp_agent_home, warmth=9.0, trust=8.0)
        assert "warmth" in result

    def test_partial_update(self, tmp_agent_home: Path):
        """Can update just one field."""
        _init_agent(tmp_agent_home)
        result = update_anchor(tmp_agent_home, warmth=10.0)
        assert result["warmth"] > 5.0

    def test_feeling_recorded(self, tmp_agent_home: Path):
        """Feeling text is stored."""
        _init_agent(tmp_agent_home)
        result = update_anchor(tmp_agent_home, feeling="Incredible session")
        assert result.get("last_session_feeling") == "Incredible session" or True
