"""Tests for the SKCapstone MCP server."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from skcapstone.mcp_server import (
    _error_response,
    _home,
    _json_response,
    call_tool,
    list_tools,
    server,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_json(result: list) -> dict | list:
    """Parse the JSON from a TextContent response list.

    Args:
        result: List of TextContent objects.

    Returns:
        Parsed object from the JSON text.
    """
    assert len(result) == 1
    return json.loads(result[0].text)


# ---------------------------------------------------------------------------
# Unit tests: helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
    """Tests for internal helper functions."""

    def test_home_resolves(self):
        """Default home resolves to ~/.skcapstone."""
        result = _home()
        assert result == Path("~/.skcapstone").expanduser()

    def test_json_response_structure(self):
        """_json_response wraps data as TextContent."""
        result = _json_response({"key": "value"})
        assert len(result) == 1
        assert result[0].type == "text"
        parsed = json.loads(result[0].text)
        assert parsed == {"key": "value"}

    def test_error_response_structure(self):
        """_error_response produces error JSON."""
        result = _error_response("something broke")
        parsed = _extract_json(result)
        assert "something broke" in parsed["error"]


# ---------------------------------------------------------------------------
# Unit tests: tool listing
# ---------------------------------------------------------------------------


class TestToolListing:
    """Tests for MCP tool definitions."""

    @pytest.mark.asyncio
    async def test_list_tools_returns_all(self):
        """list_tools returns all registered tools."""
        tools = await list_tools()
        assert len(tools) == 25

    @pytest.mark.asyncio
    async def test_tool_names(self):
        """All required tool names are registered."""
        tools = await list_tools()
        names = {t.name for t in tools}
        expected = {
            "agent_status",
            "memory_store",
            "memory_search",
            "memory_recall",
            "send_message",
            "check_inbox",
            "sync_push",
            "sync_pull",
            "coord_status",
            "coord_claim",
            "coord_complete",
            "coord_create",
            "ritual",
            "soul_show",
            "journal_write",
            "journal_read",
            "anchor_show",
            "germination",
            "agent_context",
            "session_capture",
            "trust_graph",
            "memory_curate",
            "trust_calibrate",
            "anchor_update",
            "state_diff",
        }
        assert names == expected

    @pytest.mark.asyncio
    async def test_tool_schemas_valid(self):
        """Each tool has a valid inputSchema with 'type' and 'properties'."""
        tools = await list_tools()
        for tool in tools:
            schema = tool.inputSchema
            assert schema["type"] == "object"
            assert "properties" in schema


# ---------------------------------------------------------------------------
# Unit tests: tool dispatch (call_tool)
# ---------------------------------------------------------------------------


class TestCallToolDispatch:
    """Tests for call_tool routing and error handling."""

    @pytest.mark.asyncio
    async def test_unknown_tool(self):
        """Unknown tool name returns an error response."""
        result = await call_tool("nonexistent_tool", {})
        parsed = _extract_json(result)
        assert "Unknown tool" in parsed["error"]

    @pytest.mark.asyncio
    async def test_agent_status_no_agent(self, tmp_path: Path):
        """agent_status with no initialized agent returns error."""
        with patch("skcapstone.mcp_server._home", return_value=tmp_path / "no-agent"):
            result = await call_tool("agent_status", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_agent_status_with_agent(self, initialized_agent_home: Path):
        """agent_status returns pillar states for a valid agent."""
        with patch("skcapstone.mcp_server._home", return_value=initialized_agent_home):
            result = await call_tool("agent_status", {})
        parsed = _extract_json(result)
        assert "pillars" in parsed
        assert "identity" in parsed["pillars"]
        assert "memory" in parsed["pillars"]
        assert "trust" in parsed["pillars"]
        assert "security" in parsed["pillars"]
        assert "sync" in parsed["pillars"]
        assert "is_conscious" in parsed
        assert parsed["name"] == "test-agent"


# ---------------------------------------------------------------------------
# Memory tool tests
# ---------------------------------------------------------------------------


class TestMemoryTools:
    """Tests for memory_store, memory_search, and memory_recall."""

    @pytest.mark.asyncio
    async def test_memory_store_requires_content(self, initialized_agent_home: Path):
        """memory_store without content returns error."""
        with patch("skcapstone.mcp_server._home", return_value=initialized_agent_home):
            result = await call_tool("memory_store", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_memory_search_requires_query(self, initialized_agent_home: Path):
        """memory_search without query returns error."""
        with patch("skcapstone.mcp_server._home", return_value=initialized_agent_home):
            result = await call_tool("memory_search", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_memory_recall_requires_id(self, initialized_agent_home: Path):
        """memory_recall without memory_id returns error."""
        with patch("skcapstone.mcp_server._home", return_value=initialized_agent_home):
            result = await call_tool("memory_recall", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_memory_store_and_search(self, initialized_agent_home: Path):
        """Store a memory then find it via search."""
        with patch("skcapstone.mcp_server._home", return_value=initialized_agent_home):
            store_result = await call_tool(
                "memory_store",
                {
                    "content": "The sovereign penguin remembers everything",
                    "tags": ["pengu", "test"],
                    "importance": 0.5,
                },
            )
            store_parsed = _extract_json(store_result)
            assert store_parsed["stored"] is True
            assert store_parsed["memory_id"]
            assert store_parsed["layer"] == "short-term"

            search_result = await call_tool(
                "memory_search", {"query": "sovereign penguin"}
            )
            search_parsed = _extract_json(search_result)
            assert isinstance(search_parsed, list)
            assert len(search_parsed) >= 1
            assert any("sovereign penguin" in r["content"] for r in search_parsed)

    @pytest.mark.asyncio
    async def test_memory_store_and_recall(self, initialized_agent_home: Path):
        """Store a memory then recall it by ID."""
        with patch("skcapstone.mcp_server._home", return_value=initialized_agent_home):
            store_result = await call_tool(
                "memory_store",
                {"content": "Recall me later", "importance": 0.3},
            )
            store_parsed = _extract_json(store_result)
            mid = store_parsed["memory_id"]

            recall_result = await call_tool("memory_recall", {"memory_id": mid})
            recall_parsed = _extract_json(recall_result)
            assert recall_parsed["memory_id"] == mid
            assert "Recall me later" in recall_parsed["content"]
            assert recall_parsed["access_count"] >= 1

    @pytest.mark.asyncio
    async def test_memory_recall_not_found(self, initialized_agent_home: Path):
        """memory_recall with nonexistent ID returns error."""
        with patch("skcapstone.mcp_server._home", return_value=initialized_agent_home):
            result = await call_tool("memory_recall", {"memory_id": "nonexistent123"})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_memory_store_high_importance_promotes(
        self, initialized_agent_home: Path
    ):
        """High-importance memory gets promoted to mid-term."""
        with patch("skcapstone.mcp_server._home", return_value=initialized_agent_home):
            result = await call_tool(
                "memory_store",
                {"content": "Critical penguin intel", "importance": 0.8},
            )
        parsed = _extract_json(result)
        assert parsed["stored"] is True
        assert parsed["layer"] == "mid-term"


# ---------------------------------------------------------------------------
# Coordination tool tests
# ---------------------------------------------------------------------------


class TestCoordTools:
    """Tests for coordination board MCP tools."""

    @pytest.mark.asyncio
    async def test_coord_status_empty(self, initialized_agent_home: Path):
        """coord_status on empty board returns zero tasks."""
        with patch("skcapstone.mcp_server._home", return_value=initialized_agent_home):
            result = await call_tool("coord_status", {})
        parsed = _extract_json(result)
        assert parsed["summary"]["total"] == 0
        assert parsed["tasks"] == []

    @pytest.mark.asyncio
    async def test_coord_claim_requires_params(self, initialized_agent_home: Path):
        """coord_claim without task_id and agent_name returns error."""
        with patch("skcapstone.mcp_server._home", return_value=initialized_agent_home):
            result = await call_tool("coord_claim", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_coord_complete_requires_params(self, initialized_agent_home: Path):
        """coord_complete without task_id and agent_name returns error."""
        with patch("skcapstone.mcp_server._home", return_value=initialized_agent_home):
            result = await call_tool("coord_complete", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_coord_create_requires_title(self, initialized_agent_home: Path):
        """coord_create without title returns error."""
        with patch("skcapstone.mcp_server._home", return_value=initialized_agent_home):
            result = await call_tool("coord_create", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_coord_claim_nonexistent_task(self, initialized_agent_home: Path):
        """coord_claim for a nonexistent task returns error."""
        from skcapstone.coordination import Board

        board = Board(initialized_agent_home)
        board.ensure_dirs()

        with patch("skcapstone.mcp_server._home", return_value=initialized_agent_home):
            result = await call_tool(
                "coord_claim", {"task_id": "nosuch", "agent_name": "tester"}
            )
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_coord_full_workflow(self, initialized_agent_home: Path):
        """Create a task via MCP, claim it, then complete it."""
        from skcapstone.coordination import Board

        board = Board(initialized_agent_home)
        board.ensure_dirs()

        with patch("skcapstone.mcp_server._home", return_value=initialized_agent_home):
            create_result = await call_tool(
                "coord_create",
                {
                    "title": "Test MCP task",
                    "priority": "high",
                    "tags": ["mcp", "test"],
                    "created_by": "mcp-builder",
                },
            )
            create_parsed = _extract_json(create_result)
            assert create_parsed["created"] is True
            task_id = create_parsed["task_id"]

            status_result = await call_tool("coord_status", {})
            status_parsed = _extract_json(status_result)
            assert status_parsed["summary"]["total"] == 1
            assert status_parsed["tasks"][0]["status"] == "open"

            claim_result = await call_tool(
                "coord_claim", {"task_id": task_id, "agent_name": "mcp-builder"}
            )
            claim_parsed = _extract_json(claim_result)
            assert claim_parsed["claimed"] is True
            assert claim_parsed["agent"] == "mcp-builder"

            complete_result = await call_tool(
                "coord_complete",
                {"task_id": task_id, "agent_name": "mcp-builder"},
            )
            complete_parsed = _extract_json(complete_result)
            assert complete_parsed["completed"] is True
            assert task_id in complete_parsed["completed_tasks"]


# ---------------------------------------------------------------------------
# SKComm tool tests (graceful fallback)
# ---------------------------------------------------------------------------


class TestCommTools:
    """Tests for send_message and check_inbox (SKComm may not be installed)."""

    @pytest.mark.asyncio
    async def test_send_message_requires_params(self):
        """send_message without recipient/message returns error."""
        result = await call_tool("send_message", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_check_inbox_graceful_fallback(self):
        """check_inbox returns graceful error when SKComm is unavailable."""
        result = await call_tool("check_inbox", {})
        parsed = _extract_json(result)
        # Either returns messages list or graceful error about skcomm
        assert isinstance(parsed, list) or "error" in parsed


# ---------------------------------------------------------------------------
# Sync tool tests
# ---------------------------------------------------------------------------


class TestSyncTools:
    """Tests for sync_push and sync_pull."""

    @pytest.mark.asyncio
    async def test_sync_push_no_agent(self, tmp_path: Path):
        """sync_push with no agent home returns error."""
        with patch("skcapstone.mcp_server._home", return_value=tmp_path / "nope"):
            result = await call_tool("sync_push", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_sync_pull_empty_inbox(self, initialized_agent_home: Path):
        """sync_pull with empty inbox returns zero seeds."""
        sync_dir = initialized_agent_home / "sync"
        sync_dir.mkdir(exist_ok=True)
        (sync_dir / "inbox").mkdir(exist_ok=True)
        (sync_dir / "outbox").mkdir(exist_ok=True)
        (sync_dir / "archive").mkdir(exist_ok=True)
        (sync_dir / "sync-manifest.json").write_text(
            json.dumps({"transport": "syncthing", "gpg_encrypt": False})
        )

        with patch("skcapstone.mcp_server._home", return_value=initialized_agent_home):
            result = await call_tool("sync_pull", {})
        parsed = _extract_json(result)
        assert parsed["pulled"] == 0
        assert parsed["seeds"] == []
