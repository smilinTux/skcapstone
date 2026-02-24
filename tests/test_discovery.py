"""Tests for component discovery engine."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from skcapstone.discovery import discover_all, discover_identity, discover_memory
from skcapstone.models import PillarStatus


def _no_capauth(*args, **kwargs):
    """Mock that simulates no CapAuth profile available."""
    return None


class TestIdentityDiscovery:
    """Tests for identity (CapAuth) discovery."""

    @patch("skcapstone.discovery._try_load_capauth_profile", _no_capauth)
    def test_missing_when_no_identity_dir(self, tmp_agent_home: Path):
        """Should report MISSING when no identity exists and no CapAuth."""
        state = discover_identity(tmp_agent_home)
        assert state.status in (PillarStatus.MISSING, PillarStatus.DEGRADED)

    @patch("skcapstone.discovery._try_load_capauth_profile", _no_capauth)
    def test_active_with_capauth_managed_manifest(self, tmp_agent_home: Path):
        """Should report ACTIVE when identity.json has capauth_managed=true."""
        identity_dir = tmp_agent_home / "identity"
        identity_dir.mkdir()
        identity_data = {
            "name": "test-agent",
            "email": "test@skcapstone.local",
            "fingerprint": "ABCD1234" * 5,
            "created_at": "2026-02-22T00:00:00+00:00",
            "capauth_managed": True,
        }
        (identity_dir / "identity.json").write_text(json.dumps(identity_data))

        state = discover_identity(tmp_agent_home)
        assert state.status == PillarStatus.ACTIVE
        assert state.name == "test-agent"
        assert state.fingerprint is not None

    @patch("skcapstone.discovery._try_load_capauth_profile", _no_capauth)
    def test_degraded_with_placeholder_manifest(self, tmp_agent_home: Path):
        """Should report DEGRADED when identity.json has no capauth_managed."""
        identity_dir = tmp_agent_home / "identity"
        identity_dir.mkdir()
        identity_data = {
            "name": "placeholder-agent",
            "email": "placeholder@skcapstone.local",
            "fingerprint": "ABCD1234" * 5,
            "created_at": "2026-02-22T00:00:00+00:00",
        }
        (identity_dir / "identity.json").write_text(json.dumps(identity_data))

        state = discover_identity(tmp_agent_home)
        assert state.status == PillarStatus.DEGRADED
        assert state.name == "placeholder-agent"

    @patch("skcapstone.discovery._try_load_capauth_profile", _no_capauth)
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
