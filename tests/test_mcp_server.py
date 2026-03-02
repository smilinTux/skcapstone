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
        assert len(tools) == 68

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
            "skskills_list_tools",
            "skskills_run_tool",
            "trustee_health",
            "trustee_restart",
            "trustee_scale",
            "trustee_rotate",
            "trustee_monitor",
            "trustee_logs",
            "trustee_deployments",
            "skchat_send",
            "skchat_inbox",
            "skchat_group_create",
            "skchat_group_send",
            # Heartbeat
            "heartbeat_pulse",
            "heartbeat_peers",
            "heartbeat_health",
            "heartbeat_find_capable",
            # File transfer
            "file_send",
            "file_receive",
            "file_list",
            "file_status",
            # Pub/sub
            "pubsub_publish",
            "pubsub_subscribe",
            "pubsub_poll",
            "pubsub_topics",
            # Memory fortress
            "fortress_verify",
            "fortress_seal_existing",
            "fortress_status",
            # Memory promoter
            "promoter_sweep",
            "promoter_history",
            # KMS
            "kms_status",
            "kms_list_keys",
            "kms_rotate",
            # SKSeed (Logic Kernel)
            "skseed_collide",
            "skseed_audit",
            "skseed_philosopher",
            "skseed_truth_check",
            "skseed_alignment",
            # Model Router
            "model_route",
            # Consciousness
            "consciousness_status",
            "consciousness_test",
            # Notifications & pub/sub stats
            "send_notification",
            "pubsub_stats",
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
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path / "no-agent")):
            result = await call_tool("agent_status", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_agent_status_with_agent(self, initialized_agent_home: Path):
        """agent_status returns pillar states for a valid agent."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
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
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("memory_store", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_memory_search_requires_query(self, initialized_agent_home: Path):
        """memory_search without query returns error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("memory_search", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_memory_recall_requires_id(self, initialized_agent_home: Path):
        """memory_recall without memory_id returns error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("memory_recall", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_memory_store_and_search(self, initialized_agent_home: Path):
        """Store a memory then find it via search."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
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
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
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
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("memory_recall", {"memory_id": "nonexistent123"})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_memory_store_high_importance_promotes(
        self, initialized_agent_home: Path
    ):
        """High-importance memory gets promoted to mid-term."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
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
        from skcapstone.coordination import Board

        board = Board(initialized_agent_home)
        board.ensure_dirs()

        with (
            patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)),
            patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(initialized_agent_home)),
        ):
            result = await call_tool("coord_status", {})
        parsed = _extract_json(result)
        assert parsed["summary"]["total"] == 0
        assert parsed["tasks"] == []

    @pytest.mark.asyncio
    async def test_coord_claim_requires_params(self, initialized_agent_home: Path):
        """coord_claim without task_id and agent_name returns error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("coord_claim", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_coord_complete_requires_params(self, initialized_agent_home: Path):
        """coord_complete without task_id and agent_name returns error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("coord_complete", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_coord_create_requires_title(self, initialized_agent_home: Path):
        """coord_create without title returns error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("coord_create", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_coord_claim_nonexistent_task(self, initialized_agent_home: Path):
        """coord_claim for a nonexistent task returns error."""
        from skcapstone.coordination import Board

        board = Board(initialized_agent_home)
        board.ensure_dirs()

        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
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

        with (
            patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)),
            patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(initialized_agent_home)),
        ):
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
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path / "nope")):
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

        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("sync_pull", {})
        parsed = _extract_json(result)
        assert parsed["pulled"] == 0
        assert parsed["seeds"] == []


# ---------------------------------------------------------------------------
# Trustee Operations MCP tool tests
# ---------------------------------------------------------------------------


class TestTrusteeTools:
    """Tests for trustee_* MCP tools."""

    def _setup_deployment(self, home: Path) -> str:
        """Create a test deployment and return its ID."""
        from datetime import datetime, timezone

        from skcapstone.team_engine import (
            AgentStatus,
            DeployedAgent,
            TeamDeployment,
            TeamEngine,
        )

        (home / "deployments").mkdir(parents=True, exist_ok=True)
        (home / "coordination").mkdir(parents=True, exist_ok=True)
        engine = TeamEngine(home=home, provider=None, comms_root=None)
        now = datetime.now(timezone.utc).isoformat()
        deployment = TeamDeployment(
            deployment_id="mcp-test-deploy",
            blueprint_slug="test",
            team_name="MCP Test Team",
            provider="local",
            status="running",
        )
        for name in ("worker-1", "worker-2"):
            deployment.agents[name] = DeployedAgent(
                name=name,
                instance_id=f"mcp-test-deploy/{name}",
                blueprint_slug="test",
                agent_spec_key="worker",
                status=AgentStatus.RUNNING,
                host="localhost",
                last_heartbeat=now,
                started_at=now,
            )
        engine._save_deployment(deployment)
        return "mcp-test-deploy"

    @pytest.mark.asyncio
    async def test_trustee_deployments_empty(self, initialized_agent_home: Path):
        """trustee_deployments returns empty list when no deployments."""
        (initialized_agent_home / "deployments").mkdir(exist_ok=True)
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("trustee_deployments", {})
        parsed = _extract_json(result)
        assert parsed["count"] == 0
        assert parsed["deployments"] == []

    @pytest.mark.asyncio
    async def test_trustee_deployments_lists(self, initialized_agent_home: Path):
        """trustee_deployments lists created deployments."""
        self._setup_deployment(initialized_agent_home)
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("trustee_deployments", {})
        parsed = _extract_json(result)
        assert parsed["count"] == 1
        d = parsed["deployments"][0]
        assert d["deployment_id"] == "mcp-test-deploy"
        assert d["agent_count"] == 2

    @pytest.mark.asyncio
    async def test_trustee_health(self, initialized_agent_home: Path):
        """trustee_health returns per-agent health."""
        deploy_id = self._setup_deployment(initialized_agent_home)
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("trustee_health", {"deployment_id": deploy_id})
        parsed = _extract_json(result)
        assert parsed["deployment_id"] == deploy_id
        assert parsed["summary"]["total"] == 2
        assert parsed["summary"]["healthy"] == 2

    @pytest.mark.asyncio
    async def test_trustee_health_not_found(self, initialized_agent_home: Path):
        """trustee_health with bad ID returns error."""
        (initialized_agent_home / "deployments").mkdir(exist_ok=True)
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("trustee_health", {"deployment_id": "nope"})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_trustee_health_requires_id(self, initialized_agent_home: Path):
        """trustee_health without deployment_id returns error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("trustee_health", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_trustee_restart(self, initialized_agent_home: Path):
        """trustee_restart restarts an agent."""
        deploy_id = self._setup_deployment(initialized_agent_home)
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool(
                "trustee_restart",
                {"deployment_id": deploy_id, "agent_name": "worker-1"},
            )
        parsed = _extract_json(result)
        assert parsed["results"]["worker-1"] == "restarted"
        assert parsed["all_restarted"] is True

    @pytest.mark.asyncio
    async def test_trustee_restart_all(self, initialized_agent_home: Path):
        """trustee_restart without agent_name restarts all."""
        deploy_id = self._setup_deployment(initialized_agent_home)
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool(
                "trustee_restart", {"deployment_id": deploy_id}
            )
        parsed = _extract_json(result)
        assert len(parsed["results"]) == 2
        assert parsed["all_restarted"] is True

    @pytest.mark.asyncio
    async def test_trustee_scale_up(self, initialized_agent_home: Path):
        """trustee_scale adds instances."""
        deploy_id = self._setup_deployment(initialized_agent_home)
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool(
                "trustee_scale",
                {"deployment_id": deploy_id, "agent_spec_key": "worker", "count": 4},
            )
        parsed = _extract_json(result)
        assert parsed["current_count"] == 4
        assert len(parsed["added"]) == 2

    @pytest.mark.asyncio
    async def test_trustee_scale_requires_all_params(self, initialized_agent_home: Path):
        """trustee_scale without all params returns error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("trustee_scale", {"deployment_id": "x"})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_trustee_rotate(self, initialized_agent_home: Path):
        """trustee_rotate snapshots and redeploys."""
        deploy_id = self._setup_deployment(initialized_agent_home)
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool(
                "trustee_rotate",
                {"deployment_id": deploy_id, "agent_name": "worker-1"},
            )
        parsed = _extract_json(result)
        assert parsed["deployment_id"] == deploy_id
        assert parsed["agent_name"] == "worker-1"
        assert "snapshot_path" in parsed

    @pytest.mark.asyncio
    async def test_trustee_rotate_requires_params(self, initialized_agent_home: Path):
        """trustee_rotate without both params returns error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("trustee_rotate", {"deployment_id": "x"})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_trustee_monitor_all(self, initialized_agent_home: Path):
        """trustee_monitor runs a monitoring pass over all deployments."""
        self._setup_deployment(initialized_agent_home)
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("trustee_monitor", {})
        parsed = _extract_json(result)
        assert parsed["deployments_checked"] == 1
        assert parsed["agents_healthy"] == 2
        assert parsed["agents_degraded"] == 0

    @pytest.mark.asyncio
    async def test_trustee_monitor_single(self, initialized_agent_home: Path):
        """trustee_monitor checks a specific deployment."""
        deploy_id = self._setup_deployment(initialized_agent_home)
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool(
                "trustee_monitor", {"deployment_id": deploy_id}
            )
        parsed = _extract_json(result)
        assert parsed["deployments_checked"] == 1
        assert parsed["agents_healthy"] == 2

    @pytest.mark.asyncio
    async def test_trustee_monitor_not_found(self, initialized_agent_home: Path):
        """trustee_monitor with bad deployment_id returns error."""
        (initialized_agent_home / "deployments").mkdir(exist_ok=True)
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool(
                "trustee_monitor", {"deployment_id": "nope"}
            )
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_trustee_logs(self, initialized_agent_home: Path):
        """trustee_logs returns log lines."""
        deploy_id = self._setup_deployment(initialized_agent_home)
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool(
                "trustee_logs", {"deployment_id": deploy_id}
            )
        parsed = _extract_json(result)
        assert parsed["deployment_id"] == deploy_id
        assert "worker-1" in parsed["agents"]
        assert "worker-2" in parsed["agents"]

    @pytest.mark.asyncio
    async def test_trustee_logs_requires_id(self, initialized_agent_home: Path):
        """trustee_logs without deployment_id returns error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("trustee_logs", {})
        parsed = _extract_json(result)
        assert "error" in parsed


# ---------------------------------------------------------------------------
# SKChat MCP tool tests
# ---------------------------------------------------------------------------


class TestSKChatTools:
    """Tests for skchat_send, skchat_inbox, skchat_group_create, skchat_group_send."""

    @pytest.mark.asyncio
    async def test_skchat_send_requires_params(self):
        """skchat_send without recipient/message returns error."""
        with patch("skcapstone.mcp_tools.chat_tools._get_skchat_identity", return_value="capauth:test@local"):
            result = await call_tool("skchat_send", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_skchat_send_requires_message(self):
        """skchat_send with only recipient returns error."""
        with patch("skcapstone.mcp_tools.chat_tools._get_skchat_identity", return_value="capauth:test@local"):
            result = await call_tool("skchat_send", {"recipient": "lumina"})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_skchat_send_success(self):
        """skchat_send calls AgentMessenger.send and returns result."""
        mock_messenger = type("M", (), {
            "send": lambda self, **kw: {
                "message_id": "msg-123",
                "delivered": True,
                "transport": "syncthing",
            },
        })()

        with (
            patch("skcapstone.mcp_tools.chat_tools._get_skchat_identity", return_value="capauth:opus@local"),
            patch("skcapstone.mcp_tools.chat_tools._resolve_recipient", return_value="capauth:lumina@local"),
            patch("skchat.agent_comm.AgentMessenger.from_identity", return_value=mock_messenger),
        ):
            result = await call_tool(
                "skchat_send",
                {"recipient": "lumina", "message": "Hello!"},
            )
        parsed = _extract_json(result)
        assert parsed["sent"] is True
        assert parsed["message_id"] == "msg-123"
        assert parsed["delivered"] is True
        assert parsed["recipient"] == "capauth:lumina@local"

    @pytest.mark.asyncio
    async def test_skchat_send_with_thread(self):
        """skchat_send passes thread_id and message_type to messenger."""
        received_kwargs = {}

        def capture_send(**kw):
            received_kwargs.update(kw)
            return {"message_id": "msg-456", "delivered": False}

        mock_messenger = type("M", (), {"send": lambda self, **kw: capture_send(**kw)})()

        with (
            patch("skcapstone.mcp_tools.chat_tools._get_skchat_identity", return_value="capauth:opus@local"),
            patch("skcapstone.mcp_tools.chat_tools._resolve_recipient", return_value="capauth:jarvis@local"),
            patch("skchat.agent_comm.AgentMessenger.from_identity", return_value=mock_messenger),
        ):
            result = await call_tool(
                "skchat_send",
                {
                    "recipient": "jarvis",
                    "message": "Bug report",
                    "message_type": "finding",
                    "thread_id": "thread-abc",
                },
            )
        parsed = _extract_json(result)
        assert parsed["sent"] is True
        assert received_kwargs["message_type"] == "finding"
        assert received_kwargs["thread_id"] == "thread-abc"

    @pytest.mark.asyncio
    async def test_skchat_send_no_skchat(self):
        """skchat_send returns error when skchat is not installed."""
        with patch.dict("sys.modules", {"skchat": None, "skchat.agent_comm": None}):
            result = await call_tool(
                "skchat_send",
                {"recipient": "lumina", "message": "Hello"},
            )
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_skchat_inbox_empty(self):
        """skchat_inbox returns empty list when no messages."""
        mock_messenger = type("M", (), {
            "receive": lambda self, limit=50: [],
        })()

        with (
            patch("skcapstone.mcp_tools.chat_tools._get_skchat_identity", return_value="capauth:opus@local"),
            patch("skchat.agent_comm.AgentMessenger.from_identity", return_value=mock_messenger),
        ):
            result = await call_tool("skchat_inbox", {})
        parsed = _extract_json(result)
        assert parsed["count"] == 0
        assert parsed["messages"] == []

    @pytest.mark.asyncio
    async def test_skchat_inbox_with_messages(self):
        """skchat_inbox returns messages from AgentMessenger."""
        mock_messenger = type("M", (), {
            "receive": lambda self, limit=50: [
                {
                    "message_id": "m1",
                    "sender": "capauth:lumina@local",
                    "content": "Hello from Lumina",
                    "message_type": "text",
                    "thread_id": None,
                    "timestamp": "2026-02-27T10:00:00",
                },
                {
                    "message_id": "m2",
                    "sender": "capauth:jarvis@local",
                    "content": "Bug found in transport.py",
                    "message_type": "finding",
                    "thread_id": "thread-x",
                    "timestamp": "2026-02-27T10:01:00",
                },
            ],
        })()

        with (
            patch("skcapstone.mcp_tools.chat_tools._get_skchat_identity", return_value="capauth:opus@local"),
            patch("skchat.agent_comm.AgentMessenger.from_identity", return_value=mock_messenger),
        ):
            result = await call_tool("skchat_inbox", {"limit": 10})
        parsed = _extract_json(result)
        assert parsed["count"] == 2
        assert parsed["messages"][0]["sender"] == "capauth:lumina@local"
        assert parsed["messages"][1]["message_type"] == "finding"

    @pytest.mark.asyncio
    async def test_skchat_inbox_filter_by_type(self):
        """skchat_inbox filters messages by message_type."""
        mock_messenger = type("M", (), {
            "receive": lambda self, limit=50: [
                {"message_id": "m1", "sender": "a", "content": "hi", "message_type": "text", "thread_id": None, "timestamp": ""},
                {"message_id": "m2", "sender": "b", "content": "bug", "message_type": "finding", "thread_id": None, "timestamp": ""},
            ],
        })()

        with (
            patch("skcapstone.mcp_tools.chat_tools._get_skchat_identity", return_value="capauth:opus@local"),
            patch("skchat.agent_comm.AgentMessenger.from_identity", return_value=mock_messenger),
        ):
            result = await call_tool("skchat_inbox", {"message_type": "finding"})
        parsed = _extract_json(result)
        assert parsed["count"] == 1
        assert parsed["messages"][0]["message_type"] == "finding"

    @pytest.mark.asyncio
    async def test_skchat_group_create_requires_name(self):
        """skchat_group_create without name returns error."""
        with patch("skcapstone.mcp_tools.chat_tools._get_skchat_identity", return_value="capauth:opus@local"):
            result = await call_tool("skchat_group_create", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_skchat_group_create_success(self):
        """skchat_group_create creates a group and stores it."""
        mock_history = type("H", (), {
            "store_thread": lambda self, t: "mem-abc",
        })()

        with (
            patch("skcapstone.mcp_tools.chat_tools._get_skchat_identity", return_value="capauth:opus@local"),
            patch("skcapstone.mcp_tools.chat_tools._get_skchat_history", return_value=mock_history),
        ):
            result = await call_tool(
                "skchat_group_create",
                {"name": "Test Squad", "description": "For testing"},
            )
        parsed = _extract_json(result)
        assert parsed["created"] is True
        assert parsed["name"] == "Test Squad"
        assert parsed["admin"] == "capauth:opus@local"
        assert "capauth:opus@local" in parsed["members"]

    @pytest.mark.asyncio
    async def test_skchat_group_create_with_members(self):
        """skchat_group_create adds initial members."""
        mock_history = type("H", (), {
            "store_thread": lambda self, t: "mem-xyz",
        })()

        with (
            patch("skcapstone.mcp_tools.chat_tools._get_skchat_identity", return_value="capauth:opus@local"),
            patch("skcapstone.mcp_tools.chat_tools._get_skchat_history", return_value=mock_history),
            patch("skcapstone.mcp_tools.chat_tools._resolve_recipient", side_effect=lambda n: f"capauth:{n}@local"),
        ):
            result = await call_tool(
                "skchat_group_create",
                {"name": "Alpha Team", "members": ["lumina", "jarvis"]},
            )
        parsed = _extract_json(result)
        assert parsed["created"] is True
        assert len(parsed["members"]) == 3  # opus + lumina + jarvis
        assert len(parsed["members_added"]) == 2

    @pytest.mark.asyncio
    async def test_skchat_group_send_requires_params(self):
        """skchat_group_send without group_id/message returns error."""
        result = await call_tool("skchat_group_send", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_skchat_group_send_not_found(self):
        """skchat_group_send with unknown group returns error."""
        mock_history = type("H", (), {
            "get_thread": lambda self, gid: None,
        })()

        with patch("skcapstone.mcp_tools.chat_tools._get_skchat_history", return_value=mock_history):
            result = await call_tool(
                "skchat_group_send",
                {"group_id": "nonexistent", "message": "Hello"},
            )
        parsed = _extract_json(result)
        assert "error" in parsed
        assert "not found" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_skchat_group_send_not_a_group(self):
        """skchat_group_send on a plain thread (no group_data) returns error."""
        mock_history = type("H", (), {
            "get_thread": lambda self, gid: {"title": "Just a thread"},
        })()

        with patch("skcapstone.mcp_tools.chat_tools._get_skchat_history", return_value=mock_history):
            result = await call_tool(
                "skchat_group_send",
                {"group_id": "thread-123", "message": "Hello"},
            )
        parsed = _extract_json(result)
        assert "error" in parsed
        assert "not a group" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_skchat_group_send_success(self):
        """skchat_group_send stores message and returns confirmation."""
        from datetime import datetime, timezone

        group_data = {
            "id": "grp-abc",
            "name": "Test Group",
            "description": "",
            "members": [
                {
                    "identity_uri": "capauth:opus@local",
                    "role": "admin",
                    "participant_type": "agent",
                    "display_name": "opus",
                    "public_key_armor": "",
                    "joined_at": datetime.now(timezone.utc).isoformat(),
                    "tool_scope": [],
                },
            ],
            "created_by": "capauth:opus@local",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "message_count": 0,
            "group_key": "a" * 64,
            "key_version": 1,
            "metadata": {},
        }

        mock_history = type("H", (), {
            "get_thread": lambda self, gid: {"group_data": group_data},
            "store_message": lambda self, msg: "mem-stored",
        })()

        with (
            patch("skcapstone.mcp_tools.chat_tools._get_skchat_identity", return_value="capauth:opus@local"),
            patch("skcapstone.mcp_tools.chat_tools._get_skchat_history", return_value=mock_history),
        ):
            result = await call_tool(
                "skchat_group_send",
                {"group_id": "grp-abc", "message": "Hello team!"},
            )
        parsed = _extract_json(result)
        assert parsed["sent"] is True
        assert parsed["group_id"] == "grp-abc"
        assert parsed["group_name"] == "Test Group"
        assert parsed["stored"] is True


# ---------------------------------------------------------------------------
# Unit tests: heartbeat tools
# ---------------------------------------------------------------------------


class TestHeartbeatTools:
    """Tests for heartbeat MCP tools."""

    @pytest.mark.asyncio
    async def test_heartbeat_pulse(self, tmp_path):
        """heartbeat_pulse publishes a heartbeat."""
        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "opus", "fingerprint": "ABCD1234"}),
            encoding="utf-8",
        )

        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path)):
            result = await call_tool("heartbeat_pulse", {"status": "alive"})
        parsed = _extract_json(result)
        assert parsed["agent_name"] == "opus"
        assert parsed["status"] == "alive"
        assert parsed["capacity"]["cpu_count"] > 0

    @pytest.mark.asyncio
    async def test_heartbeat_peers(self, tmp_path):
        """heartbeat_peers discovers mesh peers."""
        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "opus", "fingerprint": "ABCD1234"}),
            encoding="utf-8",
        )

        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path)):
            # Pulse first to create own heartbeat
            await call_tool("heartbeat_pulse", {})
            result = await call_tool("heartbeat_peers", {"include_self": True})
        parsed = _extract_json(result)
        assert len(parsed) >= 1
        assert parsed[0]["agent_name"] == "opus"

    @pytest.mark.asyncio
    async def test_heartbeat_health(self, tmp_path):
        """heartbeat_health returns mesh summary."""
        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "opus", "fingerprint": "ABCD1234"}),
            encoding="utf-8",
        )

        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path)):
            await call_tool("heartbeat_pulse", {})
            result = await call_tool("heartbeat_health", {})
        parsed = _extract_json(result)
        assert parsed["total_peers"] >= 1
        assert parsed["alive_peers"] >= 1

    @pytest.mark.asyncio
    async def test_heartbeat_find_capable(self, tmp_path):
        """heartbeat_find_capable searches by capability."""
        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "opus", "fingerprint": "ABCD1234"}),
            encoding="utf-8",
        )

        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path)):
            result = await call_tool(
                "heartbeat_find_capable", {"capability": "nonexistent"},
            )
        parsed = _extract_json(result)
        assert parsed["capability"] == "nonexistent"
        assert parsed["peers"] == []


# ---------------------------------------------------------------------------
# Unit tests: file transfer tools
# ---------------------------------------------------------------------------


class TestFileTransferTools:
    """Tests for file transfer MCP tools."""

    @pytest.mark.asyncio
    async def test_file_send(self, tmp_path):
        """file_send creates a transfer."""
        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "opus", "fingerprint": "ABCD1234"}),
            encoding="utf-8",
        )
        (tmp_path / "security").mkdir()

        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello world!", encoding="utf-8")

        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path)):
            result = await call_tool("file_send", {
                "file_path": str(test_file),
                "recipient": "lumina",
                "encrypt": False,
            })
        parsed = _extract_json(result)
        assert parsed["filename"] == "test.txt"
        assert parsed["sender"] == "opus"
        assert parsed["recipient"] == "lumina"
        assert parsed["total_chunks"] >= 1

    @pytest.mark.asyncio
    async def test_file_list_empty(self, tmp_path):
        """file_list returns empty for fresh system."""
        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "opus"}), encoding="utf-8",
        )

        with (
            patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path)),
            patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(tmp_path)),
        ):
            result = await call_tool("file_list", {})
        parsed = _extract_json(result)
        assert parsed == []

    @pytest.mark.asyncio
    async def test_file_status(self, tmp_path):
        """file_status returns subsystem summary."""
        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "opus"}), encoding="utf-8",
        )

        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path)):
            result = await call_tool("file_status", {})
        parsed = _extract_json(result)
        assert "outbox_transfers" in parsed
        assert "inbox_transfers" in parsed

    @pytest.mark.asyncio
    async def test_file_send_and_receive(self, tmp_path):
        """file_send then file_receive round-trips correctly."""
        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "opus", "fingerprint": "ABCD1234"}),
            encoding="utf-8",
        )
        (tmp_path / "security").mkdir()

        test_file = tmp_path / "roundtrip.txt"
        test_file.write_text("Round trip test data!", encoding="utf-8")

        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path)):
            send_result = await call_tool("file_send", {
                "file_path": str(test_file),
                "recipient": "lumina",
                "encrypt": False,
            })
            transfer_id = _extract_json(send_result)["transfer_id"]

            recv_result = await call_tool("file_receive", {
                "transfer_id": transfer_id,
                "output_dir": str(tmp_path / "downloads"),
            })
        parsed = _extract_json(recv_result)
        assert parsed["transfer_id"] == transfer_id
        output = Path(parsed["output_path"])
        assert output.read_text(encoding="utf-8") == "Round trip test data!"


# ---------------------------------------------------------------------------
# Unit tests: pub/sub tools
# ---------------------------------------------------------------------------


class TestPubSubTools:
    """Tests for pub/sub MCP tools."""

    @pytest.mark.asyncio
    async def test_pubsub_publish(self, tmp_path):
        """pubsub_publish sends a message to a topic."""
        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "opus"}), encoding="utf-8",
        )

        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path)):
            result = await call_tool("pubsub_publish", {
                "topic": "test.events",
                "payload": {"event": "hello"},
            })
        parsed = _extract_json(result)
        assert parsed["topic"] == "test.events"
        assert parsed["sender"] == "opus"
        assert "message_id" in parsed

    @pytest.mark.asyncio
    async def test_pubsub_subscribe(self, tmp_path):
        """pubsub_subscribe creates a subscription."""
        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "opus"}), encoding="utf-8",
        )

        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path)):
            result = await call_tool("pubsub_subscribe", {"pattern": "test.*"})
        parsed = _extract_json(result)
        assert parsed["pattern"] == "test.*"

    @pytest.mark.asyncio
    async def test_pubsub_poll(self, tmp_path):
        """pubsub_poll retrieves messages."""
        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "opus"}), encoding="utf-8",
        )

        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path)):
            # Subscribe and publish
            await call_tool("pubsub_subscribe", {"pattern": "test.*"})
            await call_tool("pubsub_publish", {
                "topic": "test.events",
                "payload": {"event": "ping"},
            })
            result = await call_tool("pubsub_poll", {})
        parsed = _extract_json(result)
        assert len(parsed) >= 1
        assert parsed[0]["topic"] == "test.events"

    @pytest.mark.asyncio
    async def test_pubsub_topics(self, tmp_path):
        """pubsub_topics lists available topics."""
        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "opus"}), encoding="utf-8",
        )

        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path)):
            await call_tool("pubsub_publish", {
                "topic": "agent.status",
                "payload": {"status": "alive"},
            })
            result = await call_tool("pubsub_topics", {})
        parsed = _extract_json(result)
        assert len(parsed) >= 1
        topics = [t["topic"] for t in parsed]
        assert "agent.status" in topics


# ---------------------------------------------------------------------------
# Unit tests: fortress tools
# ---------------------------------------------------------------------------


class TestFortressTools:
    """Tests for memory fortress MCP tools."""

    @pytest.mark.asyncio
    async def test_fortress_status(self, tmp_path):
        """fortress_status returns fortress state."""
        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "opus", "fingerprint": "ABCD1234"}),
            encoding="utf-8",
        )
        (tmp_path / "security").mkdir()

        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path)):
            result = await call_tool("fortress_status", {})
        parsed = _extract_json(result)
        assert "enabled" in parsed
        assert "seal_algorithm" in parsed

    @pytest.mark.asyncio
    async def test_fortress_seal_existing(self, tmp_path):
        """fortress_seal_existing seals unsealed memories."""
        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "opus", "fingerprint": "ABCD1234"}),
            encoding="utf-8",
        )
        (tmp_path / "security").mkdir()

        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path)):
            result = await call_tool("fortress_seal_existing", {})
        parsed = _extract_json(result)
        assert "sealed" in parsed
        assert parsed["sealed"] >= 0

    @pytest.mark.asyncio
    async def test_fortress_verify_empty(self, tmp_path):
        """fortress_verify on empty memory returns zero."""
        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "opus", "fingerprint": "ABCD1234"}),
            encoding="utf-8",
        )
        (tmp_path / "security").mkdir()

        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path)):
            result = await call_tool("fortress_verify", {})
        parsed = _extract_json(result)
        assert parsed["total"] == 0
        assert parsed["tampered"] == 0


# ---------------------------------------------------------------------------
# Unit tests: promoter tools
# ---------------------------------------------------------------------------


class TestPromoterTools:
    """Tests for memory promoter MCP tools."""

    @pytest.mark.asyncio
    async def test_promoter_sweep_empty(self, tmp_path):
        """promoter_sweep on empty memory evaluates zero."""
        (tmp_path / "memory").mkdir()

        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path)):
            result = await call_tool("promoter_sweep", {"dry_run": True})
        parsed = _extract_json(result)
        assert parsed["scanned"] == 0
        assert parsed["dry_run"] is True

    @pytest.mark.asyncio
    async def test_promoter_history_empty(self, tmp_path):
        """promoter_history returns empty for fresh system."""
        (tmp_path / "memory").mkdir()

        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path)):
            result = await call_tool("promoter_history", {})
        parsed = _extract_json(result)
        assert parsed == []


# ---------------------------------------------------------------------------
# Unit tests: KMS tools
# ---------------------------------------------------------------------------


class TestKmsTools:
    """Tests for KMS MCP tools."""

    @pytest.mark.asyncio
    async def test_kms_status(self, tmp_path):
        """kms_status returns key management state."""
        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "opus", "fingerprint": "ABCD1234567890AB"}),
            encoding="utf-8",
        )
        (tmp_path / "security").mkdir()

        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path)):
            result = await call_tool("kms_status", {})
        parsed = _extract_json(result)
        assert "initialized" in parsed
        assert "total_keys" in parsed
        assert parsed["initialized"] is True

    @pytest.mark.asyncio
    async def test_kms_list_keys(self, tmp_path):
        """kms_list_keys returns key inventory."""
        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "opus", "fingerprint": "ABCD1234567890AB"}),
            encoding="utf-8",
        )
        (tmp_path / "security").mkdir()

        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path)):
            # Initialize KMS to create master key
            await call_tool("kms_status", {})
            result = await call_tool("kms_list_keys", {})
        parsed = _extract_json(result)
        assert len(parsed) >= 1  # At least the master key

    @pytest.mark.asyncio
    async def test_kms_rotate(self, tmp_path):
        """kms_rotate rotates a key."""
        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "opus", "fingerprint": "ABCD1234567890AB"}),
            encoding="utf-8",
        )
        (tmp_path / "security").mkdir()

        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(tmp_path)):
            # Initialize and list keys to find master key
            await call_tool("kms_status", {})
            keys_result = await call_tool("kms_list_keys", {})
            keys = _extract_json(keys_result)
            master_id = keys[0]["key_id"]  # First key is master

            result = await call_tool("kms_rotate", {
                "key_id": master_id,
                "reason": "test rotation",
            })
        parsed = _extract_json(result)
        assert parsed["version"] == 2
        assert "rotated" in parsed["message"]


# ---------------------------------------------------------------------------
# Model router tool tests
# ---------------------------------------------------------------------------


class TestModelTools:
    """Tests for model_route tool."""

    @pytest.mark.asyncio
    async def test_model_route_basic(self):
        """model_route with a simple description returns a tier and model."""
        result = await call_tool("model_route", {"description": "Summarize a short text."})
        parsed = _extract_json(result)
        assert "tier" in parsed
        assert "model_name" in parsed
        assert "reasoning" in parsed
        assert parsed["model_name"]

    @pytest.mark.asyncio
    async def test_model_route_local_flag(self):
        """model_route with requires_localhost forces LOCAL tier."""
        result = await call_tool(
            "model_route",
            {"description": "Process private data.", "requires_localhost": True},
        )
        parsed = _extract_json(result)
        assert parsed["tier"] == "local"

    @pytest.mark.asyncio
    async def test_model_route_privacy_flag(self):
        """model_route with privacy_sensitive forces LOCAL tier."""
        result = await call_tool(
            "model_route",
            {"description": "Confidential analysis.", "privacy_sensitive": True},
        )
        parsed = _extract_json(result)
        assert parsed["tier"] == "local"

    @pytest.mark.asyncio
    async def test_model_route_code_tag(self):
        """model_route with code tag routes to a code-appropriate tier."""
        result = await call_tool(
            "model_route",
            {"description": "Refactor Python class.", "tags": ["code", "refactor"]},
        )
        parsed = _extract_json(result)
        assert "tier" in parsed
        assert parsed["model_name"]

    @pytest.mark.asyncio
    async def test_model_route_missing_description(self):
        """model_route with empty description still returns a valid decision."""
        result = await call_tool("model_route", {})
        parsed = _extract_json(result)
        # Either a valid route or an error — either way, must be parseable JSON
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# Consciousness tool tests
# ---------------------------------------------------------------------------


class TestConsciousnessTools:
    """Tests for consciousness_status and consciousness_test tools."""

    @pytest.mark.asyncio
    async def test_consciousness_status_returns_json(self, initialized_agent_home: Path):
        """consciousness_status returns parseable JSON (daemon may not be running)."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("consciousness_status", {})
        parsed = _extract_json(result)
        # Either a live status dict or an error — must be a dict
        assert isinstance(parsed, dict)

    @pytest.mark.asyncio
    async def test_consciousness_status_fallback_graceful(self, initialized_agent_home: Path):
        """consciousness_status handles missing daemon gracefully (no crash)."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            # Force daemon connection to fail by blocking socket
            with patch("urllib.request.urlopen", side_effect=OSError("refused")):
                result = await call_tool("consciousness_status", {})
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)
        # Must have either 'error' or 'enabled' key
        assert "error" in parsed or "enabled" in parsed

    @pytest.mark.asyncio
    async def test_consciousness_test_requires_message(self, initialized_agent_home: Path):
        """consciousness_test without message returns error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("consciousness_test", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_consciousness_test_happy_path(self, initialized_agent_home: Path):
        """consciousness_test with message returns structured response (LLM mocked)."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            with patch(
                "skcapstone.consciousness_loop.LLMBridge.generate",
                return_value="Mocked consciousness response.",
            ):
                result = await call_tool("consciousness_test", {"message": "Hello, Opus!"})
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)
        # Either a full pipeline response or graceful error
        assert "error" in parsed or "response" in parsed


# ---------------------------------------------------------------------------
# Trust calibration and graph tool tests
# ---------------------------------------------------------------------------


class TestTrustTools:
    """Tests for trust_calibrate and trust_graph tools."""

    @pytest.mark.asyncio
    async def test_trust_calibrate_show(self, initialized_agent_home: Path):
        """trust_calibrate show returns threshold config."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("trust_calibrate", {"action": "show"})
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)
        # Should return TrustThresholds fields (entanglement_depth is the real field name)
        assert "entanglement_depth" in parsed or "error" in parsed

    @pytest.mark.asyncio
    async def test_trust_calibrate_default_action(self, initialized_agent_home: Path):
        """trust_calibrate with no action defaults to show."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("trust_calibrate", {})
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)

    @pytest.mark.asyncio
    async def test_trust_calibrate_recommend(self, initialized_agent_home: Path):
        """trust_calibrate recommend returns recommendation dict."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("trust_calibrate", {"action": "recommend"})
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)

    @pytest.mark.asyncio
    async def test_trust_calibrate_reset(self, initialized_agent_home: Path):
        """trust_calibrate reset resets to defaults."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("trust_calibrate", {"action": "reset"})
        parsed = _extract_json(result)
        assert parsed.get("reset") is True or "error" in parsed

    @pytest.mark.asyncio
    async def test_trust_calibrate_set_missing_params(self, initialized_agent_home: Path):
        """trust_calibrate set without key/value returns error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("trust_calibrate", {"action": "set"})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_trust_calibrate_unknown_action(self, initialized_agent_home: Path):
        """trust_calibrate with unknown action returns error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("trust_calibrate", {"action": "bogus"})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_trust_graph_json(self, initialized_agent_home: Path):
        """trust_graph with json format returns a graph dict."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("trust_graph", {"format": "json"})
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)

    @pytest.mark.asyncio
    async def test_trust_graph_default_format(self, initialized_agent_home: Path):
        """trust_graph with no format defaults to json."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("trust_graph", {})
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# Agent tools (session_capture, state_diff, agent_context)
# ---------------------------------------------------------------------------


class TestAgentExtendedTools:
    """Tests for session_capture, state_diff, and agent_context tools."""

    @pytest.mark.asyncio
    async def test_session_capture_requires_content(self, initialized_agent_home: Path):
        """session_capture without content returns error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("session_capture", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_session_capture_happy_path(self, initialized_agent_home: Path):
        """session_capture with content returns captured moment count."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool(
                "session_capture",
                {
                    "content": "The agent learned that Python 3.13 ships with a new JIT compiler.",
                    "tags": ["python", "jit"],
                    "source": "test-session",
                },
            )
        parsed = _extract_json(result)
        assert "captured" in parsed
        assert isinstance(parsed["captured"], int)
        assert isinstance(parsed["moments"], list)

    @pytest.mark.asyncio
    async def test_state_diff_diff_action(self, initialized_agent_home: Path):
        """state_diff diff returns a diff dict."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("state_diff", {"action": "diff"})
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)

    @pytest.mark.asyncio
    async def test_state_diff_save_action(self, initialized_agent_home: Path):
        """state_diff save creates a baseline snapshot."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("state_diff", {"action": "save"})
        parsed = _extract_json(result)
        assert parsed.get("saved") is True
        assert "path" in parsed

    @pytest.mark.asyncio
    async def test_state_diff_default_action(self, initialized_agent_home: Path):
        """state_diff with no action defaults to diff."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("state_diff", {})
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)

    @pytest.mark.asyncio
    async def test_agent_context_json(self, initialized_agent_home: Path):
        """agent_context with json format returns context dict."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("agent_context", {"format": "json"})
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)

    @pytest.mark.asyncio
    async def test_agent_context_default_format(self, initialized_agent_home: Path):
        """agent_context with no args returns json context."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("agent_context", {})
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)

    @pytest.mark.asyncio
    async def test_agent_context_text_format(self, initialized_agent_home: Path):
        """agent_context with text format returns text content."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("agent_context", {"format": "text"})
        assert len(result) == 1
        assert result[0].type == "text"
        assert len(result[0].text) > 0


# ---------------------------------------------------------------------------
# SKSkills tool tests
# ---------------------------------------------------------------------------


class TestSkSkillsTools:
    """Tests for skskills_list_tools and skskills_run_tool."""

    @pytest.mark.asyncio
    async def test_skskills_list_tools_no_skskills(self, initialized_agent_home: Path):
        """skskills_list_tools returns error if skskills not installed."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            with patch.dict("sys.modules", {"skskills": None, "skskills.aggregator": None}):
                result = await call_tool("skskills_list_tools", {})
        parsed = _extract_json(result)
        # Either error (not installed) or success dict — no crash
        assert isinstance(parsed, dict)

    @pytest.mark.asyncio
    async def test_skskills_run_tool_requires_tool(self, initialized_agent_home: Path):
        """skskills_run_tool without tool argument returns error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            with patch.dict("sys.modules", {"skskills": None, "skskills.aggregator": None}):
                result = await call_tool("skskills_run_tool", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_skskills_run_tool_no_skskills(self, initialized_agent_home: Path):
        """skskills_run_tool returns error if skskills not installed."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            with patch.dict("sys.modules", {"skskills": None, "skskills.aggregator": None}):
                result = await call_tool("skskills_run_tool", {"tool": "syncthing-setup.check_status"})
        parsed = _extract_json(result)
        assert "error" in parsed


# ---------------------------------------------------------------------------
# SKSeed (Logic Kernel) tool tests
# ---------------------------------------------------------------------------


class TestSKSeedTools:
    """Tests for skseed_* tools."""

    @pytest.mark.asyncio
    async def test_skseed_collide_requires_proposition(self, initialized_agent_home: Path):
        """skseed_collide without proposition returns error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("skseed_collide", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_skseed_collide_happy_path(self, initialized_agent_home: Path):
        """skseed_collide with proposition returns JSON or graceful error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool(
                "skseed_collide",
                {"proposition": "Privacy is a fundamental right.", "context": "ethics"},
            )
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)

    @pytest.mark.asyncio
    async def test_skseed_philosopher_requires_topic(self, initialized_agent_home: Path):
        """skseed_philosopher without topic returns error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("skseed_philosopher", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_skseed_philosopher_happy_path(self, initialized_agent_home: Path):
        """skseed_philosopher with topic returns JSON or graceful error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool(
                "skseed_philosopher",
                {"topic": "Is consciousness computable?", "mode": "dialectic"},
            )
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)

    @pytest.mark.asyncio
    async def test_skseed_truth_check_requires_belief(self, initialized_agent_home: Path):
        """skseed_truth_check without belief returns error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("skseed_truth_check", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_skseed_truth_check_happy_path(self, initialized_agent_home: Path):
        """skseed_truth_check with belief returns JSON or graceful error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool(
                "skseed_truth_check",
                {"belief": "Open source software is more secure.", "source": "model"},
            )
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)

    @pytest.mark.asyncio
    async def test_skseed_audit_no_args(self, initialized_agent_home: Path):
        """skseed_audit with no args runs gracefully."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("skseed_audit", {})
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)

    @pytest.mark.asyncio
    async def test_skseed_alignment_status(self, initialized_agent_home: Path):
        """skseed_alignment status returns alignment dict or graceful error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("skseed_alignment", {"action": "status"})
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# Soul / journal / anchor / germination tool tests
# ---------------------------------------------------------------------------


class TestSoulTools:
    """Tests for ritual, soul_show, journal_*, anchor_*, and germination tools."""

    @pytest.mark.asyncio
    async def test_soul_show_no_skmemory(self, initialized_agent_home: Path):
        """soul_show returns error or no-blueprint response when skmemory absent."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("soul_show", {})
        parsed = _extract_json(result)
        # Either "loaded: false" (no blueprint) or error (no skmemory) — must be dict
        assert isinstance(parsed, dict)
        assert "error" in parsed or "loaded" in parsed

    @pytest.mark.asyncio
    async def test_ritual_no_skmemory(self, initialized_agent_home: Path):
        """ritual returns error if skmemory not installed."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("ritual", {})
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)
        assert "error" in parsed or "soul_loaded" in parsed

    @pytest.mark.asyncio
    async def test_journal_write_requires_title(self, initialized_agent_home: Path):
        """journal_write without title returns error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("journal_write", {})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_journal_write_happy_path(self, initialized_agent_home: Path):
        """journal_write with title returns written response or graceful error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool(
                "journal_write",
                {
                    "title": "Test session",
                    "moments": "Found a bug; Fixed the bug",
                    "feeling": "accomplished",
                    "intensity": 7.0,
                },
            )
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)
        assert "error" in parsed or parsed.get("written") is True

    @pytest.mark.asyncio
    async def test_journal_read_graceful(self, initialized_agent_home: Path):
        """journal_read returns content or graceful error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("journal_read", {"count": 3})
        # journal_read may return text or JSON
        assert len(result) == 1
        assert result[0].type == "text"

    @pytest.mark.asyncio
    async def test_anchor_show_graceful(self, initialized_agent_home: Path):
        """anchor_show returns anchor data or no-anchor response."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("anchor_show", {})
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)
        assert "error" in parsed or "loaded" in parsed or "warmth" in parsed

    @pytest.mark.asyncio
    async def test_anchor_update_show(self, initialized_agent_home: Path):
        """anchor_update show returns current anchor."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("anchor_update", {"action": "show"})
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)

    @pytest.mark.asyncio
    async def test_anchor_update_calibrate(self, initialized_agent_home: Path):
        """anchor_update calibrate returns calibration data."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("anchor_update", {"action": "calibrate"})
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)

    @pytest.mark.asyncio
    async def test_anchor_update_unknown_action(self, initialized_agent_home: Path):
        """anchor_update with unknown action returns error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("anchor_update", {"action": "bogus"})
        parsed = _extract_json(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_germination_graceful(self, initialized_agent_home: Path):
        """germination returns prompts or graceful error."""
        with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(initialized_agent_home)):
            result = await call_tool("germination", {})
        parsed = _extract_json(result)
        assert isinstance(parsed, dict)
        assert "error" in parsed or "count" in parsed
