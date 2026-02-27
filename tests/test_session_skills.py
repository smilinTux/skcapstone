"""Tests for the session skills bridge — wiring SKSkills into agent runtime sessions."""

import json
import os
from pathlib import Path
from textwrap import dedent
from typing import Dict
from unittest.mock import patch

import pytest


@pytest.fixture
def skskills_home(tmp_path: Path) -> Path:
    """Set up a mock SKSkills home with installed skills."""
    home = tmp_path / "skskills"
    installed = home / "installed"
    agents = home / "agents"

    # Global skill
    global_skill = installed / "syncthing-setup"
    global_skill.mkdir(parents=True)
    (global_skill / "skill.yaml").write_text(dedent("""\
        name: syncthing-setup
        version: "1.0.0"
        description: Auto-configure Syncthing
        author:
          name: tester
    """))

    # Agent-specific skill
    agent_skill = agents / "jarvis" / "code-review"
    agent_skill.mkdir(parents=True)
    (agent_skill / "skill.yaml").write_text(dedent("""\
        name: code-review
        version: "0.1.0"
        description: Code review skill
        author:
          name: tester
    """))

    return home


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """Set up a mock repo root with legacy OpenClaw skills."""
    root = tmp_path / "repo"
    oc_skills = root / "openclaw-skills"
    oc_skills.mkdir(parents=True)
    (oc_skills / "legacy.skill").write_text("legacy skill content")
    legacy_dir = oc_skills / "legacy-dir"
    legacy_dir.mkdir()
    (legacy_dir / "manifest.json").write_text("{}")
    return root


class TestResolveSkillPaths:
    """Test skill resolution with SKSkills registry integration."""

    def test_resolve_from_skskills_global(self, skskills_home: Path):
        """Should resolve skill from global SKSkills registry."""
        from skcapstone.session_skills import resolve_skill_paths_with_skskills

        with patch.dict(os.environ, {"SKSKILLS_HOME": str(skskills_home)}):
            result = resolve_skill_paths_with_skskills(["syncthing-setup"])
            assert len(result) == 1
            assert "syncthing-setup" in result[0]
            assert Path(result[0]).exists()

    def test_resolve_from_skskills_agent(self, skskills_home: Path):
        """Should resolve agent-specific skills first."""
        from skcapstone.session_skills import resolve_skill_paths_with_skskills

        with patch.dict(os.environ, {"SKSKILLS_HOME": str(skskills_home)}):
            result = resolve_skill_paths_with_skskills(
                ["code-review"], agent="jarvis"
            )
            assert len(result) == 1
            assert "jarvis" in result[0]
            assert "code-review" in result[0]

    def test_resolve_from_legacy_openclaw(self, skskills_home: Path, repo_root: Path):
        """Should fall back to OpenClaw paths when not in SKSkills."""
        from skcapstone.session_skills import resolve_skill_paths_with_skskills

        with patch.dict(os.environ, {"SKSKILLS_HOME": str(skskills_home)}):
            result = resolve_skill_paths_with_skskills(
                ["legacy"], repo_root=repo_root
            )
            assert len(result) == 1
            assert "openclaw-skills" in result[0]

    def test_resolve_passthrough_for_unknown(self, skskills_home: Path):
        """Unknown skills should pass through as-is."""
        from skcapstone.session_skills import resolve_skill_paths_with_skskills

        with patch.dict(os.environ, {"SKSKILLS_HOME": str(skskills_home)}):
            result = resolve_skill_paths_with_skskills(["unknown-skill"])
            assert result == ["unknown-skill"]

    def test_resolve_absolute_path(self, skskills_home: Path, tmp_path: Path):
        """Absolute paths should be used as-is if they exist."""
        from skcapstone.session_skills import resolve_skill_paths_with_skskills

        abs_path = tmp_path / "my-skill"
        abs_path.mkdir()

        with patch.dict(os.environ, {"SKSKILLS_HOME": str(skskills_home)}):
            result = resolve_skill_paths_with_skskills([str(abs_path)])
            assert result == [str(abs_path)]


class TestPrepareSessionSkills:
    """Test session skill preparation."""

    def test_prepare_with_skskills_dir(self, skskills_home: Path, tmp_path: Path):
        """Should load skills that have skill.yaml."""
        from skcapstone.session_skills import prepare_session_skills

        work_dir = tmp_path / "agent-work"
        work_dir.mkdir()

        skill_path = str(skskills_home / "installed" / "syncthing-setup")
        result = prepare_session_skills("test-agent", [skill_path], work_dir)

        assert result["skills_loaded"] == 1
        assert "syncthing-setup" in result["skill_names"]

    def test_prepare_skips_non_skskills(self, tmp_path: Path):
        """Should skip paths that don't have skill.yaml."""
        from skcapstone.session_skills import prepare_session_skills

        work_dir = tmp_path / "agent-work"
        work_dir.mkdir()
        non_skill = tmp_path / "not-a-skill"
        non_skill.mkdir()

        result = prepare_session_skills("test-agent", [str(non_skill)], work_dir)
        assert result["skills_loaded"] == 0

    def test_prepare_writes_mcp_config(self, skskills_home: Path, tmp_path: Path):
        """Should write MCP config when skills are loaded."""
        from skcapstone.session_skills import prepare_session_skills

        work_dir = tmp_path / "agent-work"
        work_dir.mkdir()

        skill_path = str(skskills_home / "installed" / "syncthing-setup")
        result = prepare_session_skills("test-agent", [skill_path], work_dir)

        assert result["mcp_config_path"] is not None
        config_path = Path(result["mcp_config_path"])
        assert config_path.exists()
        config = json.loads(config_path.read_text())
        assert "mcpServers" in config
        assert "skskills" in config["mcpServers"]


class TestEnrichConfigs:
    """Test session and crush config enrichment."""

    def test_enrich_session_config(self):
        """Should add skskills metadata to session config."""
        from skcapstone.session_skills import enrich_session_config

        session_config: Dict = {"agent_name": "test", "skills": []}
        skill_result = {
            "skills_loaded": 2,
            "skill_names": ["skill-a", "skill-b"],
            "tools_available": ["skill-a.tool1", "skill-b.tool2"],
            "mcp_config_path": "/tmp/test.json",
        }

        enriched = enrich_session_config(session_config, skill_result)
        assert "skskills" in enriched
        assert enriched["skskills"]["loaded"] == 2
        assert enriched["skskills"]["tools_available"] == ["skill-a.tool1", "skill-b.tool2"]

    def test_enrich_session_config_no_skills(self):
        """Should not add skskills section when no skills loaded."""
        from skcapstone.session_skills import enrich_session_config

        session_config: Dict = {"agent_name": "test"}
        skill_result = {"skills_loaded": 0, "skill_names": [], "tools_available": []}

        enriched = enrich_session_config(session_config, skill_result)
        assert "skskills" not in enriched

    def test_enrich_crush_config(self):
        """Should add skskills MCP server to crush config."""
        from skcapstone.session_skills import enrich_crush_config

        crush_config: Dict = {
            "options": {},
            "permissions": {"allowed_tools": ["view", "edit"]},
        }
        skill_result = {
            "skills_loaded": 1,
            "tools_available": ["my-skill.deploy"],
        }

        enriched = enrich_crush_config(crush_config, skill_result)
        assert "skskills" in enriched["mcpServers"]
        assert "mcp_skskills_my_skill_deploy" in enriched["permissions"]["allowed_tools"]

    def test_enrich_crush_config_no_skills(self):
        """Should not modify crush config when no skills loaded."""
        from skcapstone.session_skills import enrich_crush_config

        crush_config: Dict = {"options": {}}
        skill_result = {"skills_loaded": 0, "tools_available": []}

        enriched = enrich_crush_config(crush_config, skill_result)
        assert "mcpServers" not in enriched


class TestCleanup:
    """Test session skills cleanup."""

    def test_cleanup_removes_mcp_config(self, tmp_path: Path):
        """Should remove the MCP config file."""
        from skcapstone.session_skills import cleanup_session_skills

        work_dir = tmp_path / "agent-work"
        work_dir.mkdir()
        mcp_config = work_dir / "skskills_mcp.json"
        mcp_config.write_text("{}")

        cleanup_session_skills("test-agent", work_dir)
        assert not mcp_config.exists()

    def test_cleanup_handles_missing_config(self, tmp_path: Path):
        """Should not fail when MCP config doesn't exist."""
        from skcapstone.session_skills import cleanup_session_skills

        work_dir = tmp_path / "agent-work"
        work_dir.mkdir()

        # Should not raise
        cleanup_session_skills("test-agent", work_dir)
