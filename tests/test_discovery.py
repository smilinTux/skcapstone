"""Tests for component discovery engine."""

from __future__ import annotations

import json
from pathlib import Path

from skcapstone.discovery import discover_identity, discover_memory, discover_all
from skcapstone.models import PillarStatus


class TestIdentityDiscovery:
    """Tests for identity (CapAuth) discovery."""

    def test_missing_when_no_identity_dir(self, tmp_agent_home: Path):
        """Should report MISSING when no identity exists."""
        state = discover_identity(tmp_agent_home)
        # Reason: capauth may or may not be installed in test env
        assert state.status in (PillarStatus.MISSING, PillarStatus.DEGRADED)

    def test_active_with_identity_manifest(self, tmp_agent_home: Path):
        """Should report ACTIVE when identity.json exists with data."""
        identity_dir = tmp_agent_home / "identity"
        identity_dir.mkdir()
        identity_data = {
            "name": "test-agent",
            "email": "test@skcapstone.local",
            "fingerprint": "ABCD1234" * 5,
            "created_at": "2026-02-22T00:00:00+00:00",
        }
        (identity_dir / "identity.json").write_text(json.dumps(identity_data))

        state = discover_identity(tmp_agent_home)
        assert state.status == PillarStatus.ACTIVE
        assert state.name == "test-agent"
        assert state.fingerprint is not None

    def test_error_with_corrupt_manifest(self, tmp_agent_home: Path):
        """Should report ERROR when identity.json is corrupt."""
        identity_dir = tmp_agent_home / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.json").write_text("{not valid json")

        state = discover_identity(tmp_agent_home)
        assert state.status == PillarStatus.ERROR


class TestDiscoverAll:
    """Tests for full discovery sweep."""

    def test_discover_all_returns_four_pillars(self, tmp_agent_home: Path):
        """discover_all should return all four pillar states."""
        result = discover_all(tmp_agent_home)
        assert "identity" in result
        assert "memory" in result
        assert "trust" in result
        assert "security" in result
