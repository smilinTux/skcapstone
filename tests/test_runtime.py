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
