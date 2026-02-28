"""Tests for component discovery engine."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from skcapstone.discovery import discover_all, discover_identity, discover_memory, discover_skills
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


class TestDiscoverSkills:
    """Tests for SKSkills discovery."""

    def test_missing_when_no_skskills_home(self, tmp_path: Path, tmp_agent_home: Path):
        """Should report MISSING when ~/.skskills doesn't exist."""
        nonexistent = tmp_path / "no-skskills"
        with patch.dict(os.environ, {"SKSKILLS_HOME": str(nonexistent)}):
            state = discover_skills(tmp_agent_home)
        assert state.status == PillarStatus.MISSING
        assert state.installed == 0

    def test_degraded_when_home_exists_but_empty(self, tmp_path: Path, tmp_agent_home: Path):
        """Should report DEGRADED when ~/.skskills exists but has no skills."""
        skskills_home = tmp_path / "skskills"
        skskills_home.mkdir()
        with patch.dict(os.environ, {"SKSKILLS_HOME": str(skskills_home)}):
            state = discover_skills(tmp_agent_home)
        assert state.status == PillarStatus.DEGRADED
        assert state.installed == 0

    def test_active_with_installed_skills(self, tmp_path: Path, tmp_agent_home: Path):
        """Should report ACTIVE and count skills when skills are installed."""
        skskills_home = tmp_path / "skskills"
        installed = skskills_home / "installed"

        for skill_name in ("syncthing-setup", "pgp-identity"):
            skill_dir = installed / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "skill.yaml").write_text(
                f"name: {skill_name}\nversion: '0.1.0'\n"
            )

        with patch.dict(os.environ, {"SKSKILLS_HOME": str(skskills_home)}):
            state = discover_skills(tmp_agent_home)

        assert state.status == PillarStatus.ACTIVE
        assert state.installed == 2
        assert "syncthing-setup" in state.skill_names
        assert "pgp-identity" in state.skill_names

    def test_ignores_dirs_without_skill_yaml(self, tmp_path: Path, tmp_agent_home: Path):
        """Should not count directories that lack skill.yaml."""
        skskills_home = tmp_path / "skskills"
        installed = skskills_home / "installed"
        (installed / "broken-skill").mkdir(parents=True)  # no skill.yaml
        real_skill = installed / "real-skill"
        real_skill.mkdir()
        (real_skill / "skill.yaml").write_text("name: real-skill\nversion: '0.1.0'\n")

        with patch.dict(os.environ, {"SKSKILLS_HOME": str(skskills_home)}):
            state = discover_skills(tmp_agent_home)

        assert state.installed == 1
        assert state.skill_names == ["real-skill"]


class TestDiscoverAll:
    """Tests for full discovery sweep."""

    def test_discover_all_returns_six_pillars(self, tmp_agent_home: Path):
        """discover_all should return all six pillar states including skills."""
        result = discover_all(tmp_agent_home)
        assert "identity" in result
        assert "memory" in result
        assert "trust" in result
        assert "security" in result
        assert "sync" in result
        assert "skills" in result

    def test_discover_all_returns_four_pillars(self, tmp_agent_home: Path):
        """discover_all should return identity, memory, trust, security (backward compat)."""
        result = discover_all(tmp_agent_home)
        assert "identity" in result
        assert "memory" in result
        assert "trust" in result
        assert "security" in result
