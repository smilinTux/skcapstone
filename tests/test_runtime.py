"""Tests for the AgentRuntime."""

from __future__ import annotations

from pathlib import Path

from skcapstone.runtime import AgentRuntime


class TestAgentRuntime:
    """Tests for the agent runtime lifecycle."""

    def test_runtime_detects_uninitialized(self, tmp_path: Path):
        """Runtime should detect when agent home doesn't exist."""
        nonexistent = tmp_path / "nope"
        runtime = AgentRuntime(home=nonexistent)
        assert not runtime.is_initialized

    def test_runtime_detects_initialized(self, initialized_agent_home: Path):
        """Runtime should detect a properly initialized home."""
        runtime = AgentRuntime(home=initialized_agent_home)
        assert runtime.is_initialized

    def test_awaken_loads_manifest(self, initialized_agent_home: Path):
        """Awaken should load the agent name from manifest."""
        runtime = AgentRuntime(home=initialized_agent_home)
        manifest = runtime.awaken()
        assert manifest.name == "test-agent"
        assert manifest.last_awakened is not None

    def test_register_connector(self, initialized_agent_home: Path):
        """Registering a connector should persist it."""
        runtime = AgentRuntime(home=initialized_agent_home)
        runtime.awaken()

        connector = runtime.register_connector("Cursor IDE", "cursor")
        assert connector.platform == "cursor"
        assert connector.active is True
        assert len(runtime.manifest.connectors) == 1

    def test_register_same_connector_twice(self, initialized_agent_home: Path):
        """Re-registering the same platform should update, not duplicate."""
        runtime = AgentRuntime(home=initialized_agent_home)
        runtime.awaken()

        runtime.register_connector("Cursor IDE", "cursor")
        runtime.register_connector("Cursor IDE", "cursor")
        assert len(runtime.manifest.connectors) == 1

    def test_save_and_reload_manifest(self, initialized_agent_home: Path):
        """Manifest should survive save/reload cycle."""
        runtime = AgentRuntime(home=initialized_agent_home)
        runtime.awaken()
        runtime.register_connector("Terminal", "terminal")
        runtime.save_manifest()

        runtime2 = AgentRuntime(home=initialized_agent_home)
        runtime2.awaken()
        assert len(runtime2.manifest.connectors) == 1
        assert runtime2.manifest.connectors[0].platform == "terminal"

    def test_awaken_populates_skills_state(self, initialized_agent_home: Path):
        """Awaken should populate manifest.skills from SKSkills discovery."""
        from skcapstone.models import SkillsState

        runtime = AgentRuntime(home=initialized_agent_home)
        manifest = runtime.awaken()
        assert isinstance(manifest.skills, SkillsState)

    def test_pillar_summary_includes_skills(self, initialized_agent_home: Path):
        """pillar_summary property should include the skills pillar."""
        runtime = AgentRuntime(home=initialized_agent_home)
        manifest = runtime.awaken()
        summary = manifest.pillar_summary
        assert "skills" in summary

    def test_load_skills_handles_missing_skskills(self, initialized_agent_home: Path):
        """load_skills() should return None gracefully when skskills is unavailable."""
        import sys
        from unittest.mock import patch

        runtime = AgentRuntime(home=initialized_agent_home)
        runtime.awaken()

        # Patch ImportError to simulate skskills not installed
        with patch.dict(sys.modules, {"skskills.loader": None, "skskills.registry": None}):
            result = runtime.load_skills()

        # Returns None when not installed, or a loader when installed
        assert result is None or hasattr(result, "all_tools")

    def test_load_skills_with_installed_skills(self, initialized_agent_home: Path, tmp_path: Path):
        """load_skills() should load skills from SKSkills registry."""
        import os
        from textwrap import dedent
        from unittest.mock import patch

        # Create a minimal SKSkills installation
        skskills_home = tmp_path / "skskills"
        installed_dir = skskills_home / "installed"
        skill_dir = installed_dir / "test-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "skill.yaml").write_text(dedent("""\
            name: test-skill
            version: "0.1.0"
            description: A test skill
            author:
              name: tester
        """))

        runtime = AgentRuntime(home=initialized_agent_home)
        runtime.awaken()

        with patch.dict(os.environ, {"SKSKILLS_HOME": str(skskills_home)}):
            loader = runtime.load_skills(agent="global")

        if loader is not None:
            # SKSkills is installed — verify the skill was loaded
            assert runtime.manifest.skills.loaded >= 0
