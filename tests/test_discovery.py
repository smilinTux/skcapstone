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


class TestDiscoverSkillsRemoteRegistry:
    """Tests for remote skills-registry integration in discover_skills."""

    def test_remote_registry_fields_default(self, tmp_path: Path, tmp_agent_home: Path):
        """New remote fields should have safe defaults when skskills is not importable."""
        nonexistent = tmp_path / "no-skskills"
        with patch.dict(os.environ, {"SKSKILLS_HOME": str(nonexistent)}):
            state = discover_skills(tmp_agent_home)
        # Remote fields have safe defaults
        assert state.registry_available is False
        assert state.remote_skill_count == 0
        # registry_url may or may not be set depending on skskills availability

    def test_remote_registry_probed_when_skskills_available(self, tmp_path: Path, tmp_agent_home: Path):
        """When skskills is importable, remote registry fields are populated."""
        skskills_home = tmp_path / "skskills"
        skskills_home.mkdir()

        mock_index = type("MockIndex", (), {"skills": [
            type("S", (), {"name": "remote-skill-1", "version": "1.0.0"})(),
            type("S", (), {"name": "remote-skill-2", "version": "0.5.0"})(),
        ]})()

        mock_remote_cls = type("MockRemoteRegistry", (), {
            "__init__": lambda self, **kw: None,
            "fetch_index": lambda self: mock_index,
        })

        mock_module = type("MockModule", (), {
            "RemoteRegistry": mock_remote_cls,
            "DEFAULT_REGISTRY_URL": "https://skills.smilintux.org/api",
        })()

        with patch.dict(os.environ, {"SKSKILLS_HOME": str(skskills_home)}):
            with patch.dict("sys.modules", {"skskills.remote": mock_module}):
                state = discover_skills(tmp_agent_home)

        assert state.registry_url == "https://skills.smilintux.org/api"
        assert state.registry_available is True
        assert state.remote_skill_count == 2

    def test_remote_registry_unreachable_degrades_gracefully(self, tmp_path: Path, tmp_agent_home: Path):
        """When remote registry is unreachable, discovery still works."""
        skskills_home = tmp_path / "skskills"
        installed = skskills_home / "installed" / "local-skill"
        installed.mkdir(parents=True)
        (installed / "skill.yaml").write_text("name: local-skill\nversion: '0.1.0'\n")

        mock_remote_cls = type("MockRemoteRegistry", (), {
            "__init__": lambda self, **kw: None,
            "fetch_index": lambda self: (_ for _ in ()).throw(ConnectionError("offline")),
        })

        mock_module = type("MockModule", (), {
            "RemoteRegistry": mock_remote_cls,
            "DEFAULT_REGISTRY_URL": "https://skills.smilintux.org/api",
        })()

        with patch.dict(os.environ, {"SKSKILLS_HOME": str(skskills_home)}):
            with patch.dict("sys.modules", {"skskills.remote": mock_module}):
                state = discover_skills(tmp_agent_home)

        # Local skills still discovered
        assert state.installed == 1
        assert "local-skill" in state.skill_names
        assert state.status == PillarStatus.ACTIVE
        # Remote is marked unavailable
        assert state.registry_available is False
        assert state.remote_skill_count == 0

    def test_custom_registry_url_via_env(self, tmp_path: Path, tmp_agent_home: Path):
        """SKSKILLS_REGISTRY_URL env var overrides the default registry URL."""
        skskills_home = tmp_path / "skskills"
        skskills_home.mkdir()

        captured_url = {}

        def mock_init(self, **kw):
            captured_url["url"] = kw.get("registry_url", "")

        mock_remote_cls = type("MockRemoteRegistry", (), {
            "__init__": mock_init,
            "fetch_index": lambda self: type("I", (), {"skills": []})(),
        })

        mock_module = type("MockModule", (), {
            "RemoteRegistry": mock_remote_cls,
            "DEFAULT_REGISTRY_URL": "https://skills.smilintux.org/api",
        })()

        custom_url = "https://custom-registry.example.com/api"
        with patch.dict(os.environ, {
            "SKSKILLS_HOME": str(skskills_home),
            "SKSKILLS_REGISTRY_URL": custom_url,
        }):
            with patch.dict("sys.modules", {"skskills.remote": mock_module}):
                state = discover_skills(tmp_agent_home)

        assert state.registry_url == custom_url


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
