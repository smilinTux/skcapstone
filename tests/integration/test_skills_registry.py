"""Integration tests for the skills-registry subsystem.

Covers:
  - discover_skills(): listing skills from local filesystem
  - enable/disable semantics (ACTIVE vs DEGRADED vs MISSING status)
  - skskills_list_tools MCP handler: correct JSON response structure
  - skskills_run_tool MCP handler: executes tools and returns results

Run only these tests:
    pytest tests/integration/test_skills_registry.py -v

Skip slow integration tests in CI:
    pytest -m "not integration" tests/

Related coordination task: f8dfda3493c0ed72
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from textwrap import dedent
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Pre-import to ensure the module stays in sys.modules throughout the test
# run.  patch.dict(sys.modules, ...) snapshots the current state; importing
# here means the module is included in that snapshot and is never removed
# between tests (which would trigger a broken pydantic re-import).
from skcapstone.mcp_tools.skills_tools import (
    HANDLERS,
    TOOLS,
    _handle_skskills_list_tools,
    _handle_skskills_run_tool,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def skskills_home(tmp_path: Path) -> Path:
    """Populated SKSkills home directory for integration tests."""
    home = tmp_path / "skskills"

    # Global installed skills
    for skill_name, version, tags in [
        ("syncthing-setup", "1.0.0", ["sync"]),
        ("pgp-identity", "0.2.0", ["identity"]),
    ]:
        skill_dir = home / "installed" / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "skill.yaml").write_text(dedent(f"""\
            name: {skill_name}
            version: "{version}"
            description: Test skill {skill_name}
            tags: {tags}
            author:
              name: tester
        """))

    # Per-agent skill (jarvis)
    agent_skill = home / "agents" / "jarvis" / "code-review"
    agent_skill.mkdir(parents=True)
    (agent_skill / "skill.yaml").write_text(dedent("""\
        name: code-review
        version: "0.1.0"
        description: Code review skill
        tags: [review]
        author:
          name: tester
    """))

    return home


@pytest.fixture
def agent_home(tmp_path: Path) -> Path:
    """Minimal skcapstone agent home directory."""
    home = tmp_path / ".skcapstone"
    home.mkdir()
    (home / "skills").mkdir()
    return home


# ---------------------------------------------------------------------------
# TestSkillDiscovery — discover_skills() filesystem integration
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSkillDiscovery:
    """Test discover_skills() reading from real filesystem (tmp_path)."""

    def test_discovers_global_skills(self, agent_home: Path, skskills_home: Path):
        """Should list all skills in ~/.skskills/installed/."""
        from skcapstone.discovery import discover_skills

        with patch.dict(os.environ, {"SKSKILLS_HOME": str(skskills_home)}):
            with patch("skcapstone.discovery._probe_remote_registry"):
                state = discover_skills(agent_home)

        assert state.installed == 2
        assert "syncthing-setup" in state.skill_names
        assert "pgp-identity" in state.skill_names

    def test_discovers_per_agent_skills(self, agent_home: Path, skskills_home: Path):
        """Should include per-agent skills when agent is specified."""
        from skcapstone.discovery import discover_skills

        with patch.dict(os.environ, {"SKSKILLS_HOME": str(skskills_home)}):
            with patch("skcapstone.discovery._probe_remote_registry"):
                state = discover_skills(agent_home, agent="jarvis")

        assert "code-review" in state.skill_names
        assert state.installed == 3  # 2 global + 1 agent

    def test_no_duplicate_when_skill_in_both_namespaces(
        self, agent_home: Path, skskills_home: Path
    ):
        """Same skill in global + agent namespace should appear once."""
        from skcapstone.discovery import discover_skills

        # Add syncthing-setup also in jarvis agent namespace
        dup = skskills_home / "agents" / "jarvis" / "syncthing-setup"
        dup.mkdir(parents=True)
        (dup / "skill.yaml").write_text("name: syncthing-setup\nversion: '1.0.0'\n")

        with patch.dict(os.environ, {"SKSKILLS_HOME": str(skskills_home)}):
            with patch("skcapstone.discovery._probe_remote_registry"):
                state = discover_skills(agent_home, agent="jarvis")

        assert state.skill_names.count("syncthing-setup") == 1

    def test_status_active_when_skills_found(self, agent_home: Path, skskills_home: Path):
        """Status should be ACTIVE when at least one skill is installed."""
        from skcapstone.discovery import discover_skills
        from skcapstone.models import PillarStatus

        with patch.dict(os.environ, {"SKSKILLS_HOME": str(skskills_home)}):
            with patch("skcapstone.discovery._probe_remote_registry"):
                state = discover_skills(agent_home)

        assert state.status == PillarStatus.ACTIVE

    def test_status_degraded_when_home_empty(self, agent_home: Path, tmp_path: Path):
        """Status should be DEGRADED when skskills home exists but has no skills."""
        from skcapstone.discovery import discover_skills
        from skcapstone.models import PillarStatus

        empty_home = tmp_path / "empty_skskills"
        empty_home.mkdir()
        (empty_home / "installed").mkdir()

        with patch.dict(os.environ, {"SKSKILLS_HOME": str(empty_home)}):
            with patch("skcapstone.discovery._probe_remote_registry"):
                state = discover_skills(agent_home)

        assert state.status == PillarStatus.DEGRADED

    def test_status_missing_when_no_skskills_home(self, agent_home: Path, tmp_path: Path):
        """Status should be MISSING when skskills home directory does not exist."""
        from skcapstone.discovery import discover_skills
        from skcapstone.models import PillarStatus

        nonexistent = tmp_path / "no_such_dir"

        with patch.dict(os.environ, {"SKSKILLS_HOME": str(nonexistent)}):
            with patch("skcapstone.discovery._probe_remote_registry"):
                state = discover_skills(agent_home)

        assert state.status == PillarStatus.MISSING

    def test_skill_names_sorted(self, agent_home: Path, skskills_home: Path):
        """skill_names should be returned in sorted order."""
        from skcapstone.discovery import discover_skills

        with patch.dict(os.environ, {"SKSKILLS_HOME": str(skskills_home)}):
            with patch("skcapstone.discovery._probe_remote_registry"):
                state = discover_skills(agent_home)

        assert state.skill_names == sorted(state.skill_names)

    def test_per_agent_skcapstone_skills(self, agent_home: Path, skskills_home: Path):
        """Per-agent skcapstone skills (highest priority) should be discovered."""
        from skcapstone.discovery import discover_skills

        # Create per-agent skcapstone skill (priority 1)
        skcap_agent_dir = agent_home / "skills" / "agents" / "opus"
        skcap_agent_dir.mkdir(parents=True)
        local_skill = skcap_agent_dir / "local-deploy"
        local_skill.mkdir()
        (local_skill / "skill.yaml").write_text("name: local-deploy\nversion: '0.1.0'\n")

        with patch.dict(os.environ, {"SKSKILLS_HOME": str(skskills_home)}):
            with patch("skcapstone.discovery._probe_remote_registry"):
                state = discover_skills(agent_home, agent="opus")

        assert "local-deploy" in state.skill_names


# ---------------------------------------------------------------------------
# TestRegistryClientIntegration — RegistryClient with mocked HTTP
# ---------------------------------------------------------------------------


class TestRegistryClientIntegration:
    """Integration tests for RegistryClient against a mocked RemoteRegistry."""

    def _make_mock_module(self, skills: list[dict[str, Any]]) -> tuple:
        """Return (mock_sys_module, mock_remote_instance) with the given skill entries."""
        mock_entries = [MagicMock() for _ in skills]
        for entry, data in zip(mock_entries, skills):
            entry.model_dump.return_value = data

        mock_index = MagicMock()
        mock_index.skills = mock_entries

        mock_remote = MagicMock()
        mock_remote.fetch_index.return_value = mock_index
        mock_remote.search.return_value = mock_entries

        mock_module = MagicMock()
        mock_module.RemoteRegistry = MagicMock(return_value=mock_remote)
        return mock_module, mock_remote

    def test_list_skills_returns_all(self):
        """list_skills() should return one dict per registry entry."""
        from skcapstone.registry_client import RegistryClient

        catalog = [
            {"name": "syncthing-setup", "version": "1.0.0", "tags": ["sync"]},
            {"name": "pgp-identity", "version": "0.2.0", "tags": ["identity"]},
        ]
        mock_module, _ = self._make_mock_module(catalog)

        with patch.dict("sys.modules", {"skskills.remote": mock_module}):
            client = RegistryClient("https://test.example.com/api")
            result = client.list_skills()

        assert len(result) == 2
        names = {s["name"] for s in result}
        assert names == {"syncthing-setup", "pgp-identity"}

    def test_search_returns_matching_skills(self):
        """search() should delegate to RemoteRegistry.search() and return dicts."""
        from skcapstone.registry_client import RegistryClient

        sync_entry = {"name": "syncthing-setup", "version": "1.0.0", "tags": ["sync"]}
        sync_mock = MagicMock()
        sync_mock.model_dump.return_value = sync_entry

        mock_remote = MagicMock()
        mock_remote.search.return_value = [sync_mock]

        mock_module = MagicMock()
        mock_module.RemoteRegistry = MagicMock(return_value=mock_remote)

        with patch.dict("sys.modules", {"skskills.remote": mock_module}):
            client = RegistryClient("https://test.example.com/api")
            result = client.search("syncthing")

        assert len(result) == 1
        assert result[0]["name"] == "syncthing-setup"

    def test_is_available_true_when_registry_responds(self):
        """is_available() returns True when fetch_index does not raise."""
        from skcapstone.registry_client import RegistryClient

        mock_remote = MagicMock()
        mock_remote.fetch_index.return_value = MagicMock(skills=[])
        mock_module = MagicMock()
        mock_module.RemoteRegistry = MagicMock(return_value=mock_remote)

        with patch.dict("sys.modules", {"skskills.remote": mock_module}):
            client = RegistryClient("https://test.example.com/api")
            assert client.is_available() is True

    def test_is_available_false_when_registry_unreachable(self):
        """is_available() returns False when the registry is offline."""
        from skcapstone.registry_client import RegistryClient

        mock_remote = MagicMock()
        mock_remote.fetch_index.side_effect = ConnectionError("network error")
        mock_module = MagicMock()
        mock_module.RemoteRegistry = MagicMock(return_value=mock_remote)

        with patch.dict("sys.modules", {"skskills.remote": mock_module}):
            client = RegistryClient("https://test.example.com/api")
            assert client.is_available() is False

    def test_install_returns_metadata_dict(self):
        """install() should return dict with name/version/agent/install_path/status."""
        from skcapstone.registry_client import RegistryClient

        installed = MagicMock()
        installed.manifest.name = "syncthing-setup"
        installed.manifest.version = "1.0.0"
        installed.agent = "global"
        installed.install_path = "/home/user/.skskills/installed/syncthing-setup"
        installed.status.value = "installed"

        mock_remote = MagicMock()
        mock_remote.pull.return_value = installed
        mock_module = MagicMock()
        mock_module.RemoteRegistry = MagicMock(return_value=mock_remote)

        with patch.dict("sys.modules", {"skskills.remote": mock_module}):
            client = RegistryClient("https://test.example.com/api")
            result = client.install("syncthing-setup")

        assert result["name"] == "syncthing-setup"
        assert result["version"] == "1.0.0"
        assert result["agent"] == "global"
        assert "install_path" in result
        assert result["status"] == "installed"

    def test_get_skill_returns_none_when_not_found(self):
        """get_skill() returns None when the remote has no matching skill."""
        from skcapstone.registry_client import RegistryClient

        mock_remote = MagicMock()
        mock_remote.get_skill_info.return_value = None
        mock_module = MagicMock()
        mock_module.RemoteRegistry = MagicMock(return_value=mock_remote)

        with patch.dict("sys.modules", {"skskills.remote": mock_module}):
            client = RegistryClient("https://test.example.com/api")
            result = client.get_skill("nonexistent")

        assert result is None

    def test_list_skills_returns_empty_when_registry_empty(self):
        """list_skills() returns [] when no skills are in the registry."""
        from skcapstone.registry_client import RegistryClient

        mock_index = MagicMock()
        mock_index.skills = []
        mock_remote = MagicMock()
        mock_remote.fetch_index.return_value = mock_index
        mock_module = MagicMock()
        mock_module.RemoteRegistry = MagicMock(return_value=mock_remote)

        with patch.dict("sys.modules", {"skskills.remote": mock_module}):
            client = RegistryClient("https://test.example.com/api")
            result = client.list_skills()

        assert result == []


# ---------------------------------------------------------------------------
# Helpers for MCP handler tests
# ---------------------------------------------------------------------------


def _make_aggregator_module(
    tools: list[dict],
    skills: list[dict],
    skills_loaded: int,
    call_result: Any = None,
) -> MagicMock:
    """Build a mock skskills.aggregator module."""
    mock_loader = MagicMock()
    mock_loader.all_tools.return_value = tools
    mock_loader.call_tool = AsyncMock(return_value=call_result)

    mock_agg = MagicMock()
    mock_agg.load_all_skills.return_value = skills_loaded
    mock_agg.loader = mock_loader
    mock_agg.get_loaded_skills.return_value = skills

    mock_module = MagicMock()
    mock_module.SkillAggregator = MagicMock(return_value=mock_agg)
    return mock_module


# ---------------------------------------------------------------------------
# TestSkillsListToolsMCP — skskills_list_tools handler
# ---------------------------------------------------------------------------


class TestSkillsListToolsMCP:
    """Integration tests for the skskills_list_tools MCP handler."""

    @pytest.mark.asyncio
    async def test_returns_tools_list(self, tmp_path: Path):
        """skskills_list_tools should return a 'tools' list in the JSON response."""
        tools = [
            {
                "name": "syncthing-setup.check_status",
                "description": "Check Syncthing status",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ]
        mock_agg_module = _make_aggregator_module(tools=tools, skills=[], skills_loaded=1)

        with patch.dict("sys.modules", {"skskills.aggregator": mock_agg_module}):
            with patch(
                "skcapstone.mcp_tools.skills_tools._home", return_value=tmp_path
            ):
                with patch(
                    "skcapstone.mcp_tools.skills_tools._get_agent_name",
                    return_value="test-agent",
                ):
                    result = await _handle_skskills_list_tools({"agent": "test-agent"})

        assert len(result) == 1
        data = json.loads(result[0].text)
        assert "tools" in data
        assert len(data["tools"]) == 1
        assert data["tools"][0]["name"] == "syncthing-setup.check_status"

    @pytest.mark.asyncio
    async def test_returns_skills_metadata(self, tmp_path: Path):
        """skskills_list_tools should include loaded skills metadata."""
        skills = [{"name": "syncthing-setup", "version": "1.0.0"}]
        mock_agg_module = _make_aggregator_module(tools=[], skills=skills, skills_loaded=1)

        with patch.dict("sys.modules", {"skskills.aggregator": mock_agg_module}):
            with patch(
                "skcapstone.mcp_tools.skills_tools._home", return_value=tmp_path
            ):
                with patch(
                    "skcapstone.mcp_tools.skills_tools._get_agent_name",
                    return_value="test-agent",
                ):
                    result = await _handle_skskills_list_tools({})

        data = json.loads(result[0].text)
        assert data["skills_loaded"] == 1
        assert data["skills"] == [{"name": "syncthing-setup", "version": "1.0.0"}]

    @pytest.mark.asyncio
    async def test_tools_have_required_fields(self, tmp_path: Path):
        """Each tool entry must expose name, description, and inputSchema."""
        tools = [
            {
                "name": "pgp-identity.show_key",
                "description": "Show PGP public key",
                "inputSchema": {
                    "type": "object",
                    "properties": {"format": {"type": "string"}},
                },
            }
        ]
        mock_agg_module = _make_aggregator_module(tools=tools, skills=[], skills_loaded=1)

        with patch.dict("sys.modules", {"skskills.aggregator": mock_agg_module}):
            with patch(
                "skcapstone.mcp_tools.skills_tools._home", return_value=tmp_path
            ):
                with patch(
                    "skcapstone.mcp_tools.skills_tools._get_agent_name",
                    return_value="opus",
                ):
                    result = await _handle_skskills_list_tools({"agent": "opus"})

        data = json.loads(result[0].text)
        for tool in data["tools"]:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool

    @pytest.mark.asyncio
    async def test_returns_error_when_skskills_missing(self, tmp_path: Path):
        """Should return an error response when skskills is not installed.

        Setting sys.modules["skskills.aggregator"] = None causes Python to
        raise ImportError when the handler does `from skskills.aggregator import ...`.
        """
        with patch.dict("sys.modules", {"skskills.aggregator": None}):
            with patch(
                "skcapstone.mcp_tools.skills_tools._home", return_value=tmp_path
            ):
                result = await _handle_skskills_list_tools({})

        data = json.loads(result[0].text)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_agent_name_in_response(self, tmp_path: Path):
        """The response should include the agent namespace used."""
        mock_agg_module = _make_aggregator_module(tools=[], skills=[], skills_loaded=0)

        with patch.dict("sys.modules", {"skskills.aggregator": mock_agg_module}):
            with patch(
                "skcapstone.mcp_tools.skills_tools._home", return_value=tmp_path
            ):
                with patch(
                    "skcapstone.mcp_tools.skills_tools._get_agent_name",
                    return_value="jarvis",
                ):
                    result = await _handle_skskills_list_tools({"agent": "jarvis"})

        data = json.loads(result[0].text)
        assert data["agent"] == "jarvis"

    @pytest.mark.asyncio
    async def test_zero_skills_returns_empty_lists(self, tmp_path: Path):
        """When no skills are installed, tools and skills lists should be empty."""
        mock_agg_module = _make_aggregator_module(tools=[], skills=[], skills_loaded=0)

        with patch.dict("sys.modules", {"skskills.aggregator": mock_agg_module}):
            with patch(
                "skcapstone.mcp_tools.skills_tools._home", return_value=tmp_path
            ):
                with patch(
                    "skcapstone.mcp_tools.skills_tools._get_agent_name",
                    return_value="anonymous",
                ):
                    result = await _handle_skskills_list_tools({})

        data = json.loads(result[0].text)
        assert data["skills_loaded"] == 0
        assert data["tools"] == []
        assert data["skills"] == []


# ---------------------------------------------------------------------------
# TestSkillsRunToolMCP — skskills_run_tool handler
# ---------------------------------------------------------------------------


class TestSkillsRunToolMCP:
    """Integration tests for the skskills_run_tool MCP handler."""

    @pytest.mark.asyncio
    async def test_executes_tool_and_returns_json_result(self, tmp_path: Path):
        """Should call the tool and return its dict result as JSON."""
        mock_agg_module = _make_aggregator_module(
            tools=[], skills=[], skills_loaded=1,
            call_result={"status": "ok", "version": "1.9.3"},
        )

        with patch.dict("sys.modules", {"skskills.aggregator": mock_agg_module}):
            with patch(
                "skcapstone.mcp_tools.skills_tools._home", return_value=tmp_path
            ):
                with patch(
                    "skcapstone.mcp_tools.skills_tools._get_agent_name",
                    return_value="test-agent",
                ):
                    result = await _handle_skskills_run_tool(
                        {"tool": "syncthing-setup.check_status"}
                    )

        data = json.loads(result[0].text)
        assert data["status"] == "ok"
        assert data["version"] == "1.9.3"

        # Verify call_tool was invoked with the correct args
        agg_instance = mock_agg_module.SkillAggregator.return_value
        agg_instance.loader.call_tool.assert_awaited_once_with(
            "syncthing-setup.check_status", {}
        )

    @pytest.mark.asyncio
    async def test_passes_args_to_tool(self, tmp_path: Path):
        """Tool arguments should be forwarded verbatim to call_tool."""
        mock_agg_module = _make_aggregator_module(
            tools=[], skills=[], skills_loaded=1, call_result={"result": "done"}
        )

        with patch.dict("sys.modules", {"skskills.aggregator": mock_agg_module}):
            with patch(
                "skcapstone.mcp_tools.skills_tools._home", return_value=tmp_path
            ):
                with patch(
                    "skcapstone.mcp_tools.skills_tools._get_agent_name",
                    return_value="test-agent",
                ):
                    await _handle_skskills_run_tool(
                        {
                            "tool": "pgp-identity.show_key",
                            "args": {"format": "armored"},
                        }
                    )

        agg_instance = mock_agg_module.SkillAggregator.return_value
        agg_instance.loader.call_tool.assert_awaited_once_with(
            "pgp-identity.show_key", {"format": "armored"}
        )

    @pytest.mark.asyncio
    async def test_missing_tool_arg_returns_error(self, tmp_path: Path):
        """Should return an error when the 'tool' argument is absent."""
        mock_agg_module = _make_aggregator_module(
            tools=[], skills=[], skills_loaded=0, call_result=None
        )

        with patch.dict("sys.modules", {"skskills.aggregator": mock_agg_module}):
            with patch(
                "skcapstone.mcp_tools.skills_tools._home", return_value=tmp_path
            ):
                result = await _handle_skskills_run_tool({})

        data = json.loads(result[0].text)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self, tmp_path: Path):
        """Should return an error when call_tool raises KeyError (unknown tool)."""
        mock_loader = MagicMock()
        mock_loader.call_tool = AsyncMock(side_effect=KeyError("no such tool: xyz.run"))
        mock_agg = MagicMock()
        mock_agg.load_all_skills.return_value = 1
        mock_agg.loader = mock_loader
        mock_module = MagicMock()
        mock_module.SkillAggregator = MagicMock(return_value=mock_agg)

        with patch.dict("sys.modules", {"skskills.aggregator": mock_module}):
            with patch(
                "skcapstone.mcp_tools.skills_tools._home", return_value=tmp_path
            ):
                with patch(
                    "skcapstone.mcp_tools.skills_tools._get_agent_name",
                    return_value="test-agent",
                ):
                    result = await _handle_skskills_run_tool({"tool": "xyz.run"})

        data = json.loads(result[0].text)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_string_result_returned_as_plain_text(self, tmp_path: Path):
        """When the tool returns a string, response should be plain text (not JSON)."""
        mock_agg_module = _make_aggregator_module(
            tools=[], skills=[], skills_loaded=1,
            call_result="Syncthing is running.",
        )

        with patch.dict("sys.modules", {"skskills.aggregator": mock_agg_module}):
            with patch(
                "skcapstone.mcp_tools.skills_tools._home", return_value=tmp_path
            ):
                with patch(
                    "skcapstone.mcp_tools.skills_tools._get_agent_name",
                    return_value="test-agent",
                ):
                    result = await _handle_skskills_run_tool(
                        {"tool": "syncthing-setup.check_status"}
                    )

        assert result[0].text == "Syncthing is running."

    @pytest.mark.asyncio
    async def test_tool_runtime_exception_returns_error(self, tmp_path: Path):
        """Unexpected exceptions from call_tool should produce an error response."""
        mock_loader = MagicMock()
        mock_loader.call_tool = AsyncMock(side_effect=RuntimeError("daemon not running"))
        mock_agg = MagicMock()
        mock_agg.load_all_skills.return_value = 1
        mock_agg.loader = mock_loader
        mock_module = MagicMock()
        mock_module.SkillAggregator = MagicMock(return_value=mock_agg)

        with patch.dict("sys.modules", {"skskills.aggregator": mock_module}):
            with patch(
                "skcapstone.mcp_tools.skills_tools._home", return_value=tmp_path
            ):
                with patch(
                    "skcapstone.mcp_tools.skills_tools._get_agent_name",
                    return_value="test-agent",
                ):
                    result = await _handle_skskills_run_tool(
                        {"tool": "syncthing-setup.check_status"}
                    )

        data = json.loads(result[0].text)
        assert "error" in data
        assert "syncthing-setup.check_status" in data["error"]

    @pytest.mark.asyncio
    async def test_uses_specified_agent_namespace(self, tmp_path: Path):
        """SkillAggregator should be constructed with the agent from args."""
        mock_agg_module = _make_aggregator_module(
            tools=[], skills=[], skills_loaded=1, call_result={"ok": True}
        )

        with patch.dict("sys.modules", {"skskills.aggregator": mock_agg_module}):
            with patch(
                "skcapstone.mcp_tools.skills_tools._home", return_value=tmp_path
            ):
                await _handle_skskills_run_tool(
                    {"tool": "syncthing-setup.check_status", "agent": "jarvis"}
                )

        mock_agg_module.SkillAggregator.assert_called_once_with(agent="jarvis")

    @pytest.mark.asyncio
    async def test_returns_error_when_skskills_missing(self, tmp_path: Path):
        """Should return an error response when skskills is not installed."""
        with patch.dict("sys.modules", {"skskills.aggregator": None}):
            with patch(
                "skcapstone.mcp_tools.skills_tools._home", return_value=tmp_path
            ):
                result = await _handle_skskills_run_tool({"tool": "syncthing-setup.run"})

        data = json.loads(result[0].text)
        assert "error" in data


# ---------------------------------------------------------------------------
# TestMCPToolRegistration — verify TOOLS and HANDLERS are wired correctly
# ---------------------------------------------------------------------------


class TestMCPToolRegistration:
    """Verify the skills MCP tools are properly declared in TOOLS and HANDLERS."""

    def test_tools_list_contains_expected_names(self):
        """TOOLS list should declare both skskills tools."""
        tool_names = {t.name for t in TOOLS}
        assert "skskills_list_tools" in tool_names
        assert "skskills_run_tool" in tool_names

    def test_handlers_map_contains_expected_names(self):
        """HANDLERS dict should map both tool names to callable handlers."""
        assert "skskills_list_tools" in HANDLERS
        assert "skskills_run_tool" in HANDLERS
        assert callable(HANDLERS["skskills_list_tools"])
        assert callable(HANDLERS["skskills_run_tool"])

    def test_skskills_run_tool_requires_tool_arg(self):
        """skskills_run_tool inputSchema must declare 'tool' as required."""
        run_tool = next(t for t in TOOLS if t.name == "skskills_run_tool")
        assert "tool" in run_tool.inputSchema["required"]

    def test_skskills_list_tools_no_required_args(self):
        """skskills_list_tools inputSchema should have no required arguments."""
        list_tool = next(t for t in TOOLS if t.name == "skskills_list_tools")
        assert list_tool.inputSchema.get("required", []) == []

    def test_handlers_are_coroutines(self):
        """Both handlers should be async coroutine functions."""
        import asyncio

        assert asyncio.iscoroutinefunction(HANDLERS["skskills_list_tools"])
        assert asyncio.iscoroutinefunction(HANDLERS["skskills_run_tool"])
