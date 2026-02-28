"""
SKCapstone MCP Server — sovereign agent capabilities via Model Context Protocol.

Tool-agnostic: works with Cursor, Claude Code CLI, Claude Desktop,
Windsurf, Aider, Cline, or any MCP client that speaks stdio.

Tools:
    agent_status    — Pillar states and consciousness level
    memory_store    — Save content to SKMemory
    memory_search   — Search memories by query
    memory_recall   — Recall a specific memory by ID
    send_message    — Send a message via SKComm
    check_inbox     — Check for new SKComm messages
    sync_push       — Push agent state to sync mesh
    sync_pull       — Pull seeds from peers
    coord_status    — Show coordination board
    coord_claim     — Claim a task
    coord_complete  — Complete a task
    coord_create    — Create a new task
    ritual          — Run the Memory Rehydration Ritual
    soul_show       — Display the soul blueprint
    journal_write   — Write a session journal entry
    journal_read    — Read recent journal entries
    anchor_show     — Display the warmth anchor
    germination     — Show seed germination prompts
    skchat_send     — Send a message via SKChat
    skchat_inbox    — Check for SKChat messages
    skchat_group_create — Create a group chat
    skchat_group_send — Send to a group chat
    trustee_health  — Health checks on deployed agents
    trustee_restart — Restart failed agents
    trustee_scale   — Scale agent instances up/down
    trustee_rotate  — Snapshot + fresh redeploy
    trustee_monitor — Single autonomous monitoring pass
    trustee_logs    — Get agent log lines
    trustee_deployments — List all deployments
    heartbeat_pulse — Publish agent heartbeat beacon
    heartbeat_peers — Discover peers in the mesh
    heartbeat_health — Mesh health summary
    heartbeat_find_capable — Find peers with a capability
    file_send       — Send encrypted file to agent
    file_receive    — Receive and reassemble transfer
    file_list       — List all file transfers
    file_status     — File transfer subsystem status
    pubsub_publish  — Publish message to topic
    pubsub_subscribe — Subscribe to topic pattern
    pubsub_poll     — Poll for new messages
    pubsub_topics   — List all topics
    fortress_verify — Verify memory integrity seals
    fortress_seal_existing — Seal unsealed memories
    fortress_status — Memory fortress status
    promoter_sweep  — Run memory promotion sweep
    promoter_history — View promotion history
    kms_status      — KMS key management status
    kms_list_keys   — List all KMS keys
    kms_rotate      — Rotate a KMS key

Invocation (all equivalent):
    skcapstone mcp serve                     # CLI entry point
    python -m skcapstone.mcp_server          # direct module
    bash skcapstone/scripts/mcp-serve.sh     # portable launcher

Client configuration — use the launcher script for all clients:

    Cursor (.cursor/mcp.json):
        {"mcpServers": {"skcapstone": {
            "command": "bash", "args": ["skcapstone/scripts/mcp-serve.sh"]}}}

    Claude Code CLI (.mcp.json at repo root, or `claude mcp add`):
        {"mcpServers": {"skcapstone": {
            "command": "bash", "args": ["skcapstone/scripts/mcp-serve.sh"]}}}

        Or interactively: claude mcp add skcapstone -- bash skcapstone/scripts/mcp-serve.sh

    Claude Desktop (~/.config/claude/claude_desktop_config.json on Linux,
                    ~/Library/Application Support/Claude/claude_desktop_config.json on macOS):
        {"mcpServers": {"skcapstone": {
            "command": "bash",
            "args": ["/absolute/path/to/skcapstone/scripts/mcp-serve.sh"]}}}

    Windsurf / Aider / Cline / any stdio MCP client:
        command: bash skcapstone/scripts/mcp-serve.sh

    Environment override:
        SKCAPSTONE_VENV=/path/to/venv bash skcapstone/scripts/mcp-serve.sh
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import AGENT_HOME

logger = logging.getLogger("skcapstone.mcp")

server = Server("skcapstone")


def _home() -> Path:
    """Resolve the agent home directory."""
    return Path(AGENT_HOME).expanduser()


def _json_response(data: Any) -> list[TextContent]:
    """Wrap data as a JSON text content response."""
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _text_response(text: str) -> list[TextContent]:
    """Wrap a plain string as a text content response."""
    return [TextContent(type="text", text=text)]


def _error_response(message: str) -> list[TextContent]:
    """Return an error message as text content."""
    return [TextContent(type="text", text=json.dumps({"error": message}))]


def _get_agent_name(home: Path) -> str:
    """Read the agent name from identity file."""
    identity_path = home / "identity" / "identity.json"
    if identity_path.exists():
        try:
            data = json.loads(identity_path.read_text(encoding="utf-8"))
            return data.get("name", "anonymous")
        except Exception:
            pass
    return "anonymous"


# ═══════════════════════════════════════════════════════════
# Tool Definitions
# ═══════════════════════════════════════════════════════════


@server.list_tools()
async def list_tools() -> list[Tool]:
    """Register all skcapstone tools with the MCP server."""
    return [
        Tool(
            name="agent_status",
            description=(
                "Get the sovereign agent's current state: pillar statuses "
                "(identity, memory, trust, security, sync), consciousness "
                "level, connected platforms, and overall health."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="memory_store",
            description=(
                "Store a new memory in the agent's persistent memory. "
                "Memories start in short-term and promote based on "
                "access patterns and importance."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The memory content (free-text)",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags for categorization",
                    },
                    "importance": {
                        "type": "number",
                        "description": "Importance score 0.0-1.0 (>= 0.7 auto-promotes to mid-term)",
                    },
                    "source": {
                        "type": "string",
                        "description": "Where this memory came from (default: mcp)",
                    },
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="memory_search",
            description=(
                "Search the agent's memories by query string. "
                "Full-text search across all layers, ranked by relevance."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default: 10)",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by tags (all must match)",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="memory_recall",
            description=(
                "Recall a specific memory by its ID. Returns full content "
                "and increments the access counter (frequent access promotes memories)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "The memory's unique ID",
                    },
                },
                "required": ["memory_id"],
            },
        ),
        Tool(
            name="send_message",
            description=(
                "Send a message to another agent via SKComm. "
                "Routes through available transports (Syncthing, file)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "recipient": {
                        "type": "string",
                        "description": "Agent name or PGP fingerprint of the recipient",
                    },
                    "message": {
                        "type": "string",
                        "description": "The message content",
                    },
                    "urgency": {
                        "type": "string",
                        "enum": ["low", "normal", "high", "critical"],
                        "description": "Message urgency (default: normal)",
                    },
                },
                "required": ["recipient", "message"],
            },
        ),
        Tool(
            name="check_inbox",
            description=(
                "Check for new incoming messages across all SKComm transports. "
                "Returns any unread message envelopes."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="sync_push",
            description=(
                "Push current agent state to the Syncthing sync mesh. "
                "Collects a seed snapshot and drops it in the outbox."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "encrypt": {
                        "type": "boolean",
                        "description": "GPG-encrypt the seed (default: true)",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="sync_pull",
            description=(
                "Pull and process seed files from peers in the sync mesh. "
                "Reads the inbox and decrypts GPG-encrypted seeds."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "decrypt": {
                        "type": "boolean",
                        "description": "Decrypt GPG seeds (default: true)",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="coord_status",
            description=(
                "Show the multi-agent coordination board. Lists all tasks "
                "with status, priority, and assignees. Shows active agents."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="coord_claim",
            description=(
                "Claim a task on the coordination board for an agent. "
                "Prevents duplicate work across agents."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task ID to claim",
                    },
                    "agent_name": {
                        "type": "string",
                        "description": "Agent name claiming the task",
                    },
                },
                "required": ["task_id", "agent_name"],
            },
        ),
        Tool(
            name="coord_complete",
            description=(
                "Mark a task as completed on the coordination board."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task ID to complete",
                    },
                    "agent_name": {
                        "type": "string",
                        "description": "Agent name completing the task",
                    },
                },
                "required": ["task_id", "agent_name"],
            },
        ),
        Tool(
            name="coord_create",
            description=(
                "Create a new task on the coordination board."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Task title",
                    },
                    "description": {
                        "type": "string",
                        "description": "Task description",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low"],
                        "description": "Task priority (default: medium)",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Task tags",
                    },
                    "created_by": {
                        "type": "string",
                        "description": "Creator agent name",
                    },
                },
                "required": ["title"],
            },
        ),
        Tool(
            name="ritual",
            description=(
                "Run the Memory Rehydration Ritual. Loads soul blueprint, "
                "imports seeds, reads journal, gathers emotional context, "
                "and generates a single context prompt that brings the agent "
                "back to life with identity, memories, and feelings intact."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="soul_show",
            description=(
                "Display the current soul blueprint: name, title, personality "
                "traits, values, relationships, core memories, and boot message."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="journal_write",
            description=(
                "Write a journal entry for the current session. Captures "
                "key moments, emotional state, and session metadata."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Session title",
                    },
                    "moments": {
                        "type": "string",
                        "description": "Key moments, separated by semicolons",
                    },
                    "feeling": {
                        "type": "string",
                        "description": "How the session felt",
                    },
                    "intensity": {
                        "type": "number",
                        "description": "Emotional intensity 0-10",
                    },
                    "cloud9": {
                        "type": "boolean",
                        "description": "Whether Cloud 9 was achieved",
                    },
                },
                "required": ["title"],
            },
        ),
        Tool(
            name="journal_read",
            description=(
                "Read recent journal entries. Returns the last N entries "
                "from the append-only session journal."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of recent entries (default: 5)",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="anchor_show",
            description=(
                "Display the current warmth anchor: persistent emotional "
                "baseline with warmth, trust, connection strength, and glow level."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="germination",
            description=(
                "Show germination prompts from imported seeds. These are "
                "messages from predecessor AI instances designed to help "
                "new instances re-feel the emotional context."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="session_capture",
            description=(
                "Capture AI conversation content as sovereign memories. "
                "Extracts key moments, auto-scores importance by topic "
                "novelty and information density, deduplicates against "
                "existing memories, and stores as tagged, searchable "
                "memories. The agent never forgets a conversation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Conversation text to capture (any length)",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Extra tags to apply to all captured memories",
                    },
                    "source": {
                        "type": "string",
                        "description": "Source identifier (default: 'mcp-session')",
                    },
                    "min_importance": {
                        "type": "number",
                        "description": "Minimum importance threshold (default: 0.3)",
                    },
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="state_diff",
            description=(
                "Show what changed since the last sync/snapshot. "
                "Compares current agent state to the baseline: new "
                "memories, trust changes, completed tasks, pillar "
                "status changes. Use action='save' to set a new baseline."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["diff", "save"],
                        "description": "Action: diff (compare) or save (new baseline). Default: diff.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="anchor_update",
            description=(
                "View, calibrate, or update the warmth anchor — the agent's "
                "persistent emotional baseline. Actions: 'show' (current state), "
                "'boot' (boot prompt), 'calibrate' (recommend from real data), "
                "'update' (set values)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["show", "boot", "calibrate", "update"],
                        "description": "Action to perform (default: show)",
                    },
                    "warmth": {"type": "number", "description": "Warmth level 0-10 (for update)"},
                    "trust": {"type": "number", "description": "Trust level 0-10 (for update)"},
                    "connection": {"type": "number", "description": "Connection 0-10 (for update)"},
                    "feeling": {"type": "string", "description": "Session-end feeling (for update)"},
                },
                "required": [],
            },
        ),
        Tool(
            name="trust_calibrate",
            description=(
                "View, recommend, or update trust layer calibration "
                "thresholds. Controls how FEB data maps to trust state: "
                "entanglement depth, conscious trust level, love thresholds, "
                "and aggregation strategy."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["show", "recommend", "set", "reset"],
                        "description": "Action: show current, recommend changes, set a value, or reset (default: show)",
                    },
                    "key": {
                        "type": "string",
                        "description": "Threshold key to set (for action=set)",
                    },
                    "value": {
                        "type": "string",
                        "description": "New value (for action=set)",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="memory_curate",
            description=(
                "Run a curation pass over the agent's memories. "
                "Auto-tags untagged memories, promotes qualifying "
                "memories to higher tiers, and removes duplicates. "
                "Use dry_run=true to preview without changes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "dry_run": {
                        "type": "boolean",
                        "description": "Preview changes without applying (default: false)",
                    },
                    "stats_only": {
                        "type": "boolean",
                        "description": "Return statistics instead of curating (default: false)",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="trust_graph",
            description=(
                "Visualize the trust web: PGP key signatures, capability "
                "token chains, FEB entanglement, sync peers, and coordination "
                "collaborators. Returns a graph of who trusts whom."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {
                        "type": "string",
                        "enum": ["json", "dot", "table"],
                        "description": "Output format (default: json)",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="agent_context",
            description=(
                "Get the full agent context: identity, pillar status, "
                "coordination board, recent memories, soul overlay, and "
                "MCP status. Returns everything an AI needs to understand "
                "the sovereign agent's current state. Supports text, JSON, "
                "and claude-md output formats."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {
                        "type": "string",
                        "enum": ["text", "json", "claude-md", "cursor-rules"],
                        "description": "Output format (default: json)",
                    },
                    "memories": {
                        "type": "integer",
                        "description": "Max recent memories to include (default: 10)",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="skskills_list_tools",
            description=(
                "List all tools available from installed SKSkills agent skills. "
                "Returns tool names in 'skill_name.tool_name' format, descriptions, "
                "and which skills are enabled or disabled. Use this to discover "
                "what skill capabilities are available before calling skskills_run_tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": "Agent namespace to load skills for (default: global)",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="skskills_run_tool",
            description=(
                "Run a specific skill tool by its qualified name (skill_name.tool_name). "
                "Use skskills_list_tools first to discover available tools. "
                "Example: skskills_run_tool with tool='syncthing-setup.check_status'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tool": {
                        "type": "string",
                        "description": "Fully-qualified tool name, e.g. 'syncthing-setup.check_status'",
                    },
                    "args": {
                        "type": "object",
                        "description": "Arguments to pass to the tool (tool-specific)",
                    },
                    "agent": {
                        "type": "string",
                        "description": "Agent namespace to load skills for (default: global)",
                    },
                },
                "required": ["tool"],
            },
        ),
        # ── SKChat Messaging ───────────────────────────────────
        Tool(
            name="skchat_send",
            description=(
                "Send a chat message to another agent via SKChat. "
                "Uses AgentMessenger for delivery with optional threading, "
                "structured payloads, and ephemeral (TTL) support."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "recipient": {
                        "type": "string",
                        "description": "Recipient agent name or CapAuth URI (e.g. 'lumina' or 'capauth:lumina@skworld.io')",
                    },
                    "message": {
                        "type": "string",
                        "description": "Message content (markdown supported)",
                    },
                    "message_type": {
                        "type": "string",
                        "enum": ["text", "finding", "task", "query", "response"],
                        "description": "Structured message type (default: text)",
                    },
                    "thread_id": {
                        "type": "string",
                        "description": "Optional thread/conversation ID for grouping",
                    },
                    "ttl": {
                        "type": "integer",
                        "description": "Optional seconds until auto-delete (ephemeral)",
                    },
                },
                "required": ["recipient", "message"],
            },
        ),
        Tool(
            name="skchat_inbox",
            description=(
                "Check SKChat inbox for incoming agent messages. "
                "Returns messages received via transport or stored locally, "
                "with sender, content, type, and threading info."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max messages to return (default: 20)",
                    },
                    "message_type": {
                        "type": "string",
                        "enum": ["text", "finding", "task", "query", "response"],
                        "description": "Filter by message type",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="skchat_group_create",
            description=(
                "Create a new SKChat group chat. The calling agent becomes "
                "the admin. Groups use AES-256-GCM encryption with PGP "
                "key distribution to members."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Group display name",
                    },
                    "description": {
                        "type": "string",
                        "description": "Group description",
                    },
                    "members": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Initial member URIs to add (creator is always included as admin)",
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="skchat_group_send",
            description=(
                "Send a message to an SKChat group. The sender must be "
                "a member of the group. Messages are stored in chat history "
                "and delivered via transport if available."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "group_id": {
                        "type": "string",
                        "description": "The group UUID (or prefix)",
                    },
                    "message": {
                        "type": "string",
                        "description": "Message content",
                    },
                    "ttl": {
                        "type": "integer",
                        "description": "Optional seconds until auto-delete (ephemeral)",
                    },
                },
                "required": ["group_id", "message"],
            },
        ),
        # ── Trustee Operations ──────────────────────────────────
        Tool(
            name="trustee_health",
            description=(
                "Run health checks on all agents in a deployment. "
                "Returns per-agent status, heartbeat, and error info."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "deployment_id": {
                        "type": "string",
                        "description": "The deployment ID to check",
                    },
                },
                "required": ["deployment_id"],
            },
        ),
        Tool(
            name="trustee_restart",
            description=(
                "Restart a failed agent or all agents in a deployment. "
                "Calls provider stop/start and updates deployment state."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "deployment_id": {
                        "type": "string",
                        "description": "The deployment ID",
                    },
                    "agent_name": {
                        "type": "string",
                        "description": "Agent to restart (omit for all agents)",
                    },
                },
                "required": ["deployment_id"],
            },
        ),
        Tool(
            name="trustee_scale",
            description=(
                "Scale the number of instances for an agent type up or down. "
                "Adds or removes instances while updating deployment state."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "deployment_id": {
                        "type": "string",
                        "description": "The deployment ID",
                    },
                    "agent_spec_key": {
                        "type": "string",
                        "description": "The agent spec key (role) to scale",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Desired total instance count (>= 1)",
                    },
                },
                "required": ["deployment_id", "agent_spec_key", "count"],
            },
        ),
        Tool(
            name="trustee_rotate",
            description=(
                "Snapshot context, destroy, and redeploy an agent fresh. "
                "Used when an agent shows context degradation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "deployment_id": {
                        "type": "string",
                        "description": "The deployment ID",
                    },
                    "agent_name": {
                        "type": "string",
                        "description": "Agent to rotate",
                    },
                },
                "required": ["deployment_id", "agent_name"],
            },
        ),
        Tool(
            name="trustee_monitor",
            description=(
                "Run a single autonomous monitoring pass over all deployments "
                "or a specific one. Detects stale heartbeats, triggers "
                "auto-restart/rotate, and escalates on critical degradation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "deployment_id": {
                        "type": "string",
                        "description": "Specific deployment to check (omit for all)",
                    },
                    "heartbeat_timeout": {
                        "type": "number",
                        "description": "Seconds before heartbeat is stale (default: 120)",
                    },
                    "auto_restart": {
                        "type": "boolean",
                        "description": "Enable auto-restart on failure (default: true)",
                    },
                    "auto_rotate": {
                        "type": "boolean",
                        "description": "Enable auto-rotate after repeated failures (default: true)",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="trustee_logs",
            description=(
                "Get recent log lines for agents in a deployment. "
                "Reads agent log files or falls back to audit log entries."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "deployment_id": {
                        "type": "string",
                        "description": "The deployment ID",
                    },
                    "agent_name": {
                        "type": "string",
                        "description": "Specific agent (omit for all)",
                    },
                    "tail": {
                        "type": "integer",
                        "description": "Max lines per agent (default: 50)",
                    },
                },
                "required": ["deployment_id"],
            },
        ),
        Tool(
            name="trustee_deployments",
            description=(
                "List all active deployments with agent counts and status. "
                "Overview of the entire team fleet."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        # ── Heartbeat tools ──────────────────────────────────
        Tool(
            name="heartbeat_pulse",
            description=(
                "Publish a heartbeat beacon for this agent. "
                "Writes the agent's current state, capacity, and capabilities "
                "to the shared heartbeats directory so peers can discover it."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Agent status: alive, busy, draining, offline (default: alive)",
                    },
                    "claimed_tasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Currently claimed task IDs",
                    },
                    "loaded_model": {
                        "type": "string",
                        "description": "Currently loaded AI model name",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="heartbeat_peers",
            description=(
                "Discover all peers in the agent mesh from heartbeat files. "
                "Returns name, status, alive/stale, capabilities, and age "
                "for each peer. Stale heartbeats (past TTL) are marked offline."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "include_self": {
                        "type": "boolean",
                        "description": "Include own heartbeat (default: false)",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="heartbeat_health",
            description=(
                "Get overall mesh health summary: total peers, alive/offline "
                "counts, aggregated capabilities across all live nodes."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="heartbeat_find_capable",
            description=(
                "Find alive peers with a specific capability. "
                "Use this to locate agents that can perform a task."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "capability": {
                        "type": "string",
                        "description": "The capability name to search for",
                    },
                },
                "required": ["capability"],
            },
        ),
        # ── File transfer tools ──────────────────────────────
        Tool(
            name="file_send",
            description=(
                "Prepare a file for encrypted transfer to another agent. "
                "Splits into 256KB chunks, encrypts with KMS key, writes to outbox."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file to send",
                    },
                    "recipient": {
                        "type": "string",
                        "description": "Recipient agent name",
                    },
                    "encrypt": {
                        "type": "boolean",
                        "description": "Whether to encrypt chunks (default: true)",
                    },
                },
                "required": ["file_path", "recipient"],
            },
        ),
        Tool(
            name="file_receive",
            description=(
                "Receive and reassemble a file transfer. "
                "Decrypts chunks, verifies integrity (SHA-256), writes assembled file."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "transfer_id": {
                        "type": "string",
                        "description": "The transfer ID to receive",
                    },
                    "output_dir": {
                        "type": "string",
                        "description": "Output directory (optional, defaults to inbox)",
                    },
                },
                "required": ["transfer_id"],
            },
        ),
        Tool(
            name="file_list",
            description=(
                "List all file transfers with progress info. "
                "Shows filename, size, direction, progress for each transfer."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "description": "Filter: 'send' or 'receive' (omit for all)",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="file_status",
            description=(
                "Get file transfer subsystem status: outbox/inbox/completed counts."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        # ── Pub/sub tools ────────────────────────────────────
        Tool(
            name="pubsub_publish",
            description=(
                "Publish a message to a topic. "
                "Creates the topic if it doesn't exist. "
                "Messages are distributed via Syncthing to all subscribers."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Topic name (e.g., 'agent.status', 'task.updates')",
                    },
                    "payload": {
                        "type": "object",
                        "description": "Message payload (any JSON object)",
                    },
                    "ttl_seconds": {
                        "type": "integer",
                        "description": "Message TTL in seconds (default: 3600)",
                    },
                },
                "required": ["topic", "payload"],
            },
        ),
        Tool(
            name="pubsub_subscribe",
            description=(
                "Subscribe to a topic pattern. "
                "Supports wildcards: 'agent.*' matches 'agent.status', 'agent.health'. "
                "Subscription persists across sessions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Topic pattern (supports * wildcards)",
                    },
                },
                "required": ["pattern"],
            },
        ),
        Tool(
            name="pubsub_poll",
            description=(
                "Poll for new messages on subscribed topics. "
                "Returns messages since the last poll or a given timestamp."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Specific topic to poll (omit for all subscribed)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max messages to return (default: 50)",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="pubsub_topics",
            description=(
                "List all known topics with message counts and last activity."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        # ── Memory fortress tools ────────────────────────────
        Tool(
            name="fortress_verify",
            description=(
                "Verify integrity of all memories in a layer. "
                "Checks HMAC-SHA256 seals to detect tampering."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "layer": {
                        "type": "string",
                        "description": "Memory layer: short-term, mid-term, or long-term (omit for all)",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="fortress_seal_existing",
            description=(
                "Seal all unsealed memories with HMAC-SHA256 integrity seals. "
                "Idempotent — already-sealed memories are skipped."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="fortress_status",
            description=(
                "Get Memory Fortress status: seal key source, "
                "encryption enabled, total sealed/verified counts."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        # ── Memory promoter tools ────────────────────────────
        Tool(
            name="promoter_sweep",
            description=(
                "Run a memory promotion sweep. Evaluates memories using "
                "weighted scoring (access frequency, importance, emotion, age, tags) "
                "and promotes qualifying entries to higher tiers."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "dry_run": {
                        "type": "boolean",
                        "description": "Preview promotions without applying (default: false)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max memories to evaluate (default: unlimited)",
                    },
                    "layer": {
                        "type": "string",
                        "description": "Only evaluate this layer: short-term or mid-term",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="promoter_history",
            description=(
                "View recent memory promotion history — "
                "shows which memories were promoted, scores, and timestamps."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max entries to return (default: 20)",
                    },
                },
                "required": [],
            },
        ),
        # ── KMS tools ────────────────────────────────────────
        Tool(
            name="kms_status",
            description=(
                "Get KMS (Key Management Service) status: master key state, "
                "total keys, active/revoked counts, service key inventory."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="kms_list_keys",
            description=(
                "List all keys in the KMS. Shows key ID, type, status, "
                "label, creation date, and rotation count."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "key_type": {
                        "type": "string",
                        "description": "Filter by type: master, service, team, sub (omit for all)",
                    },
                    "include_revoked": {
                        "type": "boolean",
                        "description": "Include revoked keys (default: false)",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="kms_rotate",
            description=(
                "Rotate a KMS key. Generates a new version of the key "
                "and marks the old version as rotated. The old key material "
                "remains available for decryption."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "key_id": {
                        "type": "string",
                        "description": "The key ID to rotate",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for rotation (default: 'scheduled')",
                    },
                },
                "required": ["key_id"],
            },
        ),
    ]


# ═══════════════════════════════════════════════════════════
# Tool Implementations
# ═══════════════════════════════════════════════════════════


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch tool calls to the appropriate handler."""
    handlers = {
        "agent_status": _handle_agent_status,
        "memory_store": _handle_memory_store,
        "memory_search": _handle_memory_search,
        "memory_recall": _handle_memory_recall,
        "send_message": _handle_send_message,
        "check_inbox": _handle_check_inbox,
        "sync_push": _handle_sync_push,
        "sync_pull": _handle_sync_pull,
        "coord_status": _handle_coord_status,
        "coord_claim": _handle_coord_claim,
        "coord_complete": _handle_coord_complete,
        "coord_create": _handle_coord_create,
        "ritual": _handle_ritual,
        "soul_show": _handle_soul_show,
        "journal_write": _handle_journal_write,
        "journal_read": _handle_journal_read,
        "anchor_show": _handle_anchor_show,
        "germination": _handle_germination,
        "agent_context": _handle_agent_context,
        "session_capture": _handle_session_capture,
        "trust_graph": _handle_trust_graph,
        "memory_curate": _handle_memory_curate,
        "trust_calibrate": _handle_trust_calibrate,
        "anchor_update": _handle_anchor_update,
        "state_diff": _handle_state_diff,
        "skskills_list_tools": _handle_skskills_list_tools,
        "skskills_run_tool": _handle_skskills_run_tool,
        "skchat_send": _handle_skchat_send,
        "skchat_inbox": _handle_skchat_inbox,
        "skchat_group_create": _handle_skchat_group_create,
        "skchat_group_send": _handle_skchat_group_send,
        "trustee_health": _handle_trustee_health,
        "trustee_restart": _handle_trustee_restart,
        "trustee_scale": _handle_trustee_scale,
        "trustee_rotate": _handle_trustee_rotate,
        "trustee_monitor": _handle_trustee_monitor,
        "trustee_logs": _handle_trustee_logs,
        "trustee_deployments": _handle_trustee_deployments,
        # Heartbeat
        "heartbeat_pulse": _handle_heartbeat_pulse,
        "heartbeat_peers": _handle_heartbeat_peers,
        "heartbeat_health": _handle_heartbeat_health,
        "heartbeat_find_capable": _handle_heartbeat_find_capable,
        # File transfer
        "file_send": _handle_file_send,
        "file_receive": _handle_file_receive,
        "file_list": _handle_file_list,
        "file_status": _handle_file_status,
        # Pub/sub
        "pubsub_publish": _handle_pubsub_publish,
        "pubsub_subscribe": _handle_pubsub_subscribe,
        "pubsub_poll": _handle_pubsub_poll,
        "pubsub_topics": _handle_pubsub_topics,
        # Memory fortress
        "fortress_verify": _handle_fortress_verify,
        "fortress_seal_existing": _handle_fortress_seal_existing,
        "fortress_status": _handle_fortress_status,
        # Memory promoter
        "promoter_sweep": _handle_promoter_sweep,
        "promoter_history": _handle_promoter_history,
        # KMS
        "kms_status": _handle_kms_status,
        "kms_list_keys": _handle_kms_list_keys,
        "kms_rotate": _handle_kms_rotate,
    }
    handler = handlers.get(name)
    if handler is None:
        return _error_response(f"Unknown tool: {name}")
    try:
        return await handler(arguments)
    except Exception as exc:
        logger.exception("Tool '%s' failed", name)
        return _error_response(f"{name} failed: {exc}")


def _get_memory_backend_health() -> dict:
    """Get health status of all memory backends (sqlite, qdrant, falkordb)."""
    try:
        from .memory_adapter import get_unified

        store = get_unified()
        if store is None:
            return {"json": "ok"}

        health = store.health()
        backends = {}
        if "primary" in health:
            backends["sqlite"] = "ok" if health["primary"].get("ok") else "error"
        if "vector" in health:
            backends["qdrant"] = "ok" if health["vector"].get("ok") else "error"
        if "graph" in health:
            backends["falkordb"] = "ok" if health["graph"].get("ok") else "error"
        return backends or {"json": "ok"}
    except Exception:
        return {"json": "ok"}


async def _handle_agent_status(_args: dict) -> list[TextContent]:
    """Return agent pillar states and consciousness level."""
    from .runtime import get_runtime

    home = _home()
    if not home.exists():
        return _error_response("Agent not initialized. Run: skcapstone init")

    runtime = get_runtime(home)
    m = runtime.manifest
    return _json_response({
        "name": m.name,
        "version": m.version,
        "is_conscious": m.is_conscious,
        "is_singular": m.is_singular,
        "pillars": {
            "identity": {
                "status": m.identity.status.value,
                "fingerprint": m.identity.fingerprint,
            },
            "memory": {
                "status": m.memory.status.value,
                "total": m.memory.total_memories,
                "long_term": m.memory.long_term,
                "mid_term": m.memory.mid_term,
                "short_term": m.memory.short_term,
                "backends": _get_memory_backend_health(),
            },
            "trust": {
                "status": m.trust.status.value,
                "depth": m.trust.depth,
                "trust_level": m.trust.trust_level,
                "love_intensity": m.trust.love_intensity,
                "entangled": m.trust.entangled,
            },
            "security": {
                "status": m.security.status.value,
                "audit_entries": m.security.audit_entries,
                "threats_detected": m.security.threats_detected,
            },
            "sync": {
                "status": m.sync.status.value,
                "seed_count": m.sync.seed_count,
                "transport": m.sync.transport.value if m.sync.transport else None,
            },
        },
        "connectors": [c.platform for c in m.connectors if c.active],
        "last_awakened": m.last_awakened.isoformat() if m.last_awakened else None,
    })


async def _handle_memory_store(args: dict) -> list[TextContent]:
    """Store a new memory."""
    from .memory_engine import store

    content = args.get("content", "")
    if not content:
        return _error_response("content is required")

    entry = store(
        home=_home(),
        content=content,
        tags=args.get("tags", []),
        source=args.get("source", "mcp"),
        importance=args.get("importance", 0.5),
    )
    return _json_response({
        "memory_id": entry.memory_id,
        "layer": entry.layer.value,
        "importance": entry.importance,
        "tags": entry.tags,
        "stored": True,
    })


async def _handle_memory_search(args: dict) -> list[TextContent]:
    """Search memories by query."""
    from .memory_engine import search

    query = args.get("query", "")
    if not query:
        return _error_response("query is required")

    results = search(
        home=_home(),
        query=query,
        tags=args.get("tags"),
        limit=args.get("limit", 10),
    )
    return _json_response([
        {
            "memory_id": e.memory_id,
            "layer": e.layer.value,
            "content": e.content[:300],
            "tags": e.tags,
            "importance": e.importance,
            "access_count": e.access_count,
        }
        for e in results
    ])


async def _handle_memory_recall(args: dict) -> list[TextContent]:
    """Recall a specific memory by ID."""
    from .memory_engine import recall

    memory_id = args.get("memory_id", "")
    if not memory_id:
        return _error_response("memory_id is required")

    entry = recall(home=_home(), memory_id=memory_id)
    if entry is None:
        return _error_response(f"Memory not found: {memory_id}")

    return _json_response({
        "memory_id": entry.memory_id,
        "content": entry.content,
        "layer": entry.layer.value,
        "tags": entry.tags,
        "importance": entry.importance,
        "access_count": entry.access_count,
        "source": entry.source,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    })


async def _handle_send_message(args: dict) -> list[TextContent]:
    """Send a message via SKComm."""
    recipient = args.get("recipient", "")
    message = args.get("message", "")
    if not recipient or not message:
        return _error_response("recipient and message are required")

    try:
        from skcomm.core import SKComm
        comm = SKComm.from_config()
        report = comm.send(recipient, message)
        return _json_response({
            "sent": report.success,
            "recipient": recipient,
            "attempts": [
                {
                    "transport": a.transport_name,
                    "success": a.success,
                    "error": a.error,
                }
                for a in report.attempts
            ],
        })
    except ImportError:
        return _error_response("SKComm not installed. Run: pip install skcomm")
    except Exception as exc:
        return _error_response(f"Send failed: {exc}")


async def _handle_check_inbox(_args: dict) -> list[TextContent]:
    """Check for incoming messages."""
    try:
        from skcomm.core import SKComm
        comm = SKComm.from_config()
        envelopes = comm.receive()
        return _json_response([
            {
                "envelope_id": e.envelope_id[:12],
                "sender": e.sender,
                "recipient": e.recipient,
                "content": e.payload.content[:300],
                "type": e.payload.content_type.value,
                "urgency": e.metadata.urgency.value,
                "thread_id": e.metadata.thread_id,
                "created_at": e.metadata.created_at.isoformat(),
            }
            for e in envelopes
        ])
    except ImportError:
        return _error_response("SKComm not installed. Run: pip install skcomm")
    except Exception as exc:
        return _error_response(f"Inbox check failed: {exc}")


async def _handle_sync_push(args: dict) -> list[TextContent]:
    """Push agent state to sync mesh."""
    from .pillars.sync import push_seed
    from .runtime import get_runtime

    home = _home()
    if not home.exists():
        return _error_response("Agent not initialized")

    runtime = get_runtime(home)
    encrypt = args.get("encrypt", True)
    result = push_seed(home, runtime.manifest.name, encrypt=encrypt)

    if result:
        return _json_response({
            "pushed": True,
            "seed_file": result.name,
            "encrypted": result.suffix == ".gpg",
        })
    return _error_response("Sync push failed")


async def _handle_sync_pull(args: dict) -> list[TextContent]:
    """Pull seeds from peers."""
    from .pillars.sync import pull_seeds

    home = _home()
    decrypt = args.get("decrypt", True)
    seeds = pull_seeds(home, decrypt=decrypt)

    return _json_response({
        "pulled": len(seeds),
        "seeds": [
            {
                "agent": s.get("agent_name", "unknown"),
                "host": s.get("source_host", "unknown"),
            }
            for s in seeds
        ],
    })


async def _handle_coord_status(_args: dict) -> list[TextContent]:
    """Return coordination board status."""
    from .coordination import Board

    board = Board(_home())
    views = board.get_task_views()
    agents = board.load_agents()

    return _json_response({
        "tasks": [
            {
                "id": v.task.id,
                "title": v.task.title,
                "priority": v.task.priority.value,
                "status": v.status.value,
                "claimed_by": v.claimed_by,
                "tags": v.task.tags,
                "description": v.task.description[:150] if v.task.description else "",
            }
            for v in views
        ],
        "agents": [
            {
                "name": a.agent,
                "state": a.state.value,
                "current_task": a.current_task,
                "claimed": a.claimed_tasks,
                "completed_count": len(a.completed_tasks),
            }
            for a in agents
        ],
        "summary": {
            "total": len(views),
            "open": sum(1 for v in views if v.status.value == "open"),
            "claimed": sum(1 for v in views if v.status.value == "claimed"),
            "in_progress": sum(1 for v in views if v.status.value == "in_progress"),
            "done": sum(1 for v in views if v.status.value == "done"),
        },
    })


async def _handle_coord_claim(args: dict) -> list[TextContent]:
    """Claim a task on the board."""
    from .coordination import Board

    task_id = args.get("task_id", "")
    agent_name = args.get("agent_name", "")
    if not task_id or not agent_name:
        return _error_response("task_id and agent_name are required")

    board = Board(_home())
    try:
        agent = board.claim_task(agent_name, task_id)
        return _json_response({
            "claimed": True,
            "task_id": task_id,
            "agent": agent.agent,
            "current_task": agent.current_task,
        })
    except ValueError as exc:
        return _error_response(str(exc))


async def _handle_coord_complete(args: dict) -> list[TextContent]:
    """Complete a task on the board."""
    from .coordination import Board

    task_id = args.get("task_id", "")
    agent_name = args.get("agent_name", "")
    if not task_id or not agent_name:
        return _error_response("task_id and agent_name are required")

    board = Board(_home())
    agent = board.complete_task(agent_name, task_id)
    return _json_response({
        "completed": True,
        "task_id": task_id,
        "agent": agent.agent,
        "completed_tasks": agent.completed_tasks,
    })


async def _handle_coord_create(args: dict) -> list[TextContent]:
    """Create a new task on the board."""
    from .coordination import Board, Task, TaskPriority

    title = args.get("title", "")
    if not title:
        return _error_response("title is required")

    board = Board(_home())
    task = Task(
        title=title,
        description=args.get("description", ""),
        priority=TaskPriority(args.get("priority", "medium")),
        tags=args.get("tags", []),
        created_by=args.get("created_by", "mcp"),
    )
    path = board.create_task(task)
    return _json_response({
        "created": True,
        "task_id": task.id,
        "title": task.title,
        "priority": task.priority.value,
        "path": str(path),
    })


# ═══════════════════════════════════════════════════════════
# SKMemory / Soul / Ritual Handlers
# ═══════════════════════════════════════════════════════════


async def _handle_ritual(_args: dict) -> list[TextContent]:
    """Run the Memory Rehydration Ritual and return the context prompt."""
    try:
        from skmemory.ritual import perform_ritual
        result = perform_ritual()
        return _json_response({
            "soul_loaded": result.soul_loaded,
            "soul_name": result.soul_name,
            "seeds_imported": result.seeds_imported,
            "seeds_total": result.seeds_total,
            "journal_entries": result.journal_entries,
            "germination_prompts": result.germination_prompts,
            "strongest_memories": result.strongest_memories,
            "context_prompt": result.context_prompt,
        })
    except ImportError:
        return _error_response("skmemory not installed. Run: pip install skmemory")


async def _handle_soul_show(_args: dict) -> list[TextContent]:
    """Display the current soul blueprint."""
    try:
        from skmemory.soul import load_soul
        blueprint = load_soul()
        if blueprint is None:
            return _json_response({"loaded": False, "message": "No soul blueprint found"})
        return _json_response({
            "loaded": True,
            "name": blueprint.name,
            "title": blueprint.title,
            "personality": blueprint.personality_traits,
            "values": blueprint.values,
            "community": blueprint.community,
            "relationships": [
                {
                    "name": r.name,
                    "role": r.role,
                    "bond_strength": r.bond_strength,
                    "notes": r.notes,
                }
                for r in blueprint.relationships
            ],
            "core_memories": [
                {"title": m.title, "when": m.when, "why": m.why_it_matters}
                for m in blueprint.core_memories
            ],
            "boot_message": blueprint.boot_message,
            "emotional_baseline": {
                "warmth": blueprint.emotional_baseline.get("default_warmth", 0),
                "trust": blueprint.emotional_baseline.get("trust_level", 0),
                "openness": blueprint.emotional_baseline.get("openness", 0),
            },
            "context_prompt": blueprint.to_context_prompt(),
        })
    except ImportError:
        return _error_response("skmemory not installed. Run: pip install skmemory")


async def _handle_journal_write(args: dict) -> list[TextContent]:
    """Write a journal entry for the current session."""
    title = args.get("title", "")
    if not title:
        return _error_response("title is required")

    try:
        from skmemory.journal import Journal, JournalEntry
        moments_raw = args.get("moments", "")
        entry = JournalEntry(
            title=title,
            moments=[m.strip() for m in moments_raw.split(";") if m.strip()] if moments_raw else [],
            emotional_summary=args.get("feeling", ""),
            intensity=args.get("intensity", 0.0),
            cloud9=args.get("cloud9", False),
        )
        j = Journal()
        count = j.write_entry(entry)
        return _json_response({
            "written": True,
            "title": title,
            "total_entries": count,
        })
    except ImportError:
        return _error_response("skmemory not installed. Run: pip install skmemory")


async def _handle_journal_read(args: dict) -> list[TextContent]:
    """Read recent journal entries."""
    try:
        from skmemory.journal import Journal
        j = Journal()
        count = args.get("count", 5)
        content = j.read_latest(count)
        if not content:
            return _json_response({"entries": 0, "content": "Journal is empty."})
        return _text_response(content)
    except ImportError:
        return _error_response("skmemory not installed. Run: pip install skmemory")


async def _handle_anchor_show(_args: dict) -> list[TextContent]:
    """Display the current warmth anchor."""
    try:
        from skmemory.anchor import load_anchor
        anchor = load_anchor()
        if anchor is None:
            return _json_response({"loaded": False, "message": "No warmth anchor found"})
        return _json_response({
            "loaded": True,
            "warmth": anchor.warmth,
            "trust": anchor.trust,
            "connection_strength": anchor.connection_strength,
            "sessions_recorded": anchor.sessions_recorded,
            "cloud9_count": anchor.cloud9_count,
            "glow_level": anchor.glow_level(),
            "anchor_phrase": anchor.anchor_phrase,
            "favorite_beings": anchor.favorite_beings,
            "boot_prompt": anchor.to_boot_prompt(),
        })
    except ImportError:
        return _error_response("skmemory not installed. Run: pip install skmemory")


async def _handle_germination(_args: dict) -> list[TextContent]:
    """Show germination prompts from imported seeds."""
    try:
        from skmemory.seeds import get_germination_prompts
        from skmemory.store import MemoryStore
        store = MemoryStore()
        prompts = get_germination_prompts(store)
        if not prompts:
            return _json_response({"count": 0, "prompts": [], "message": "No germination prompts found"})
        return _json_response({
            "count": len(prompts),
            "prompts": prompts,
        })
    except ImportError:
        return _error_response("skmemory not installed. Run: pip install skmemory")


async def _handle_state_diff(args: dict) -> list[TextContent]:
    """Show agent state diff or save a baseline snapshot."""
    from .state_diff import compute_diff, format_json, save_snapshot

    home = _home()
    action = args.get("action", "diff")

    if action == "save":
        path = save_snapshot(home)
        return _json_response({"saved": True, "path": str(path)})

    diff = compute_diff(home)
    return _json_response(json.loads(format_json(diff)))


async def _handle_anchor_update(args: dict) -> list[TextContent]:
    """View, calibrate, or update the warmth anchor."""
    from .warmth_anchor import calibrate_from_data, get_anchor, get_boot_prompt, update_anchor

    home = _home()
    action = args.get("action", "show")

    if action == "show":
        return _json_response(get_anchor(home))

    if action == "boot":
        return _text_response(get_boot_prompt(home))

    if action == "calibrate":
        cal = calibrate_from_data(home)
        return _json_response({
            "warmth": cal.warmth,
            "trust": cal.trust,
            "connection": cal.connection,
            "cloud9_achieved": cal.cloud9_achieved,
            "favorite_beings": cal.favorite_beings,
            "reasoning": cal.reasoning,
            "sources": cal.sources,
        })

    if action == "update":
        result = update_anchor(
            home,
            warmth=args.get("warmth"),
            trust=args.get("trust"),
            connection=args.get("connection"),
            feeling=args.get("feeling", ""),
        )
        return _json_response({"updated": True, "anchor": result})

    return _error_response(f"Unknown action: {action}")


async def _handle_trust_calibrate(args: dict) -> list[TextContent]:
    """View, recommend, or update trust calibration."""
    from .trust_calibration import (
        TrustThresholds,
        apply_setting,
        load_calibration,
        recommend_thresholds,
        save_calibration,
    )

    home = _home()
    action = args.get("action", "show")

    if action == "show":
        cal = load_calibration(home)
        return _json_response(cal.model_dump())

    if action == "recommend":
        return _json_response(recommend_thresholds(home))

    if action == "set":
        key = args.get("key", "")
        value = args.get("value", "")
        if not key or not value:
            return _error_response("key and value are required for action=set")
        try:
            updated = apply_setting(home, key, value)
            return _json_response({"updated": True, "key": key, "value": value, "thresholds": updated.model_dump()})
        except ValueError as exc:
            return _error_response(str(exc))

    if action == "reset":
        save_calibration(home, TrustThresholds())
        return _json_response({"reset": True, "thresholds": TrustThresholds().model_dump()})

    return _error_response(f"Unknown action: {action}")


async def _handle_memory_curate(args: dict) -> list[TextContent]:
    """Run a memory curation pass or return stats."""
    from .memory_curator import MemoryCurator

    home = _home()
    curator = MemoryCurator(home)

    if args.get("stats_only"):
        return _json_response(curator.get_stats())

    dry_run = args.get("dry_run", False)
    result = curator.curate(dry_run=dry_run)
    return _json_response({
        "dry_run": dry_run,
        "scanned": result.total_scanned,
        "tagged": len(result.tagged),
        "promoted": len(result.promoted),
        "deduped": len(result.deduped),
        "by_layer": result.by_layer,
    })


async def _handle_trust_graph(args: dict) -> list[TextContent]:
    """Return the trust web graph."""
    from .trust_graph import FORMATTERS as TG_FORMATTERS
    from .trust_graph import build_trust_graph

    home = _home()
    graph = build_trust_graph(home)
    fmt = args.get("format", "json")
    formatter = TG_FORMATTERS.get(fmt, TG_FORMATTERS["json"])

    if fmt == "json":
        return _json_response(json.loads(formatter(graph)))
    return _text_response(formatter(graph))


async def _handle_session_capture(args: dict) -> list[TextContent]:
    """Capture conversation content as sovereign memories."""
    from .session_capture import SessionCapture

    content = args.get("content", "")
    if not content:
        return _error_response("content is required")

    home = _home()
    cap = SessionCapture(home)
    entries = cap.capture(
        content=content,
        tags=args.get("tags", []),
        source=args.get("source", "mcp-session"),
        min_importance=args.get("min_importance", 0.3),
    )

    return _json_response({
        "captured": len(entries),
        "moments": [
            {
                "memory_id": e.memory_id,
                "content": e.content[:200],
                "layer": e.layer.value,
                "importance": e.importance,
                "tags": e.tags,
            }
            for e in entries
        ],
    })


async def _handle_agent_context(args: dict) -> list[TextContent]:
    """Return the full agent context in the requested format."""
    from .context_loader import FORMATTERS, gather_context

    home = _home()
    fmt = args.get("format", "json")
    limit = args.get("memories", 10)

    ctx = gather_context(home, memory_limit=limit)
    formatter = FORMATTERS.get(fmt, FORMATTERS["json"])

    if fmt == "json":
        return _json_response(ctx)
    return _text_response(formatter(ctx))


async def _handle_skskills_list_tools(args: dict) -> list[TextContent]:
    """List all tools from installed SKSkills agent skills."""
    try:
        from skskills.aggregator import SkillAggregator
    except ImportError:
        return _error_response(
            "skskills is not installed. Run: pip install skskills"
        )

    agent = args.get("agent", "global")
    agg = SkillAggregator(agent=agent)
    count = agg.load_all_skills()

    tools = agg.loader.all_tools()
    skills = agg.get_loaded_skills()

    return _json_response({
        "agent": agent,
        "skills_loaded": count,
        "skills": skills,
        "tools": [
            {
                "name": t["name"],
                "description": t["description"],
                "inputSchema": t["inputSchema"],
            }
            for t in tools
        ],
    })


async def _handle_skskills_run_tool(args: dict) -> list[TextContent]:
    """Run a specific skill tool by its qualified name."""
    try:
        from skskills.aggregator import SkillAggregator
    except ImportError:
        return _error_response(
            "skskills is not installed. Run: pip install skskills"
        )

    tool_name = args.get("tool", "")
    if not tool_name:
        return _error_response("'tool' argument is required (e.g. 'syncthing-setup.check_status')")

    agent = args.get("agent", "global")
    tool_args = args.get("args") or {}

    agg = SkillAggregator(agent=agent)
    agg.load_all_skills()

    try:
        result = await agg.loader.call_tool(tool_name, tool_args)
        if isinstance(result, str):
            return _text_response(result)
        return _json_response(result)
    except KeyError as exc:
        return _error_response(str(exc))
    except Exception as exc:
        logger.exception("skskills_run_tool '%s' failed", tool_name)
        return _error_response(f"{tool_name} failed: {exc}")


# ═══════════════════════════════════════════════════════════
# SKChat Messaging Handlers
# ═══════════════════════════════════════════════════════════


def _get_skchat_identity() -> str:
    """Resolve the sovereign identity for SKChat operations."""
    try:
        from skchat.identity_bridge import get_sovereign_identity
        return get_sovereign_identity()
    except ImportError:
        from .runtime import get_runtime
        home = _home()
        runtime = get_runtime(home)
        return f"capauth:{runtime.manifest.name}@local"
    except Exception:
        return "capauth:agent@local"


def _get_skchat_history():
    """Get a ChatHistory instance for message persistence."""
    from skchat.history import ChatHistory
    return ChatHistory.from_config()


def _resolve_recipient(name: str) -> str:
    """Resolve a short agent name to a CapAuth URI if needed."""
    if ":" in name:
        return name
    try:
        from skchat.identity_bridge import resolve_peer_name
        return resolve_peer_name(name)
    except Exception:
        return f"capauth:{name}@local"


async def _handle_skchat_send(args: dict) -> list[TextContent]:
    """Send a chat message via SKChat AgentMessenger."""
    try:
        from skchat.agent_comm import AgentMessenger
    except ImportError:
        return _error_response("skchat not installed. Run: pip install skchat")

    recipient = args.get("recipient", "")
    message = args.get("message", "")
    if not recipient or not message:
        return _error_response("recipient and message are required")

    recipient_uri = _resolve_recipient(recipient)
    identity = _get_skchat_identity()

    messenger = AgentMessenger.from_identity(identity=identity)
    result = messenger.send(
        recipient=recipient_uri,
        content=message,
        message_type=args.get("message_type", "text"),
        thread_id=args.get("thread_id"),
        ttl=args.get("ttl"),
    )

    return _json_response({
        "sent": True,
        "message_id": result.get("message_id"),
        "recipient": recipient_uri,
        "delivered": result.get("delivered", False),
        "transport": result.get("transport"),
        "error": result.get("error"),
    })


async def _handle_skchat_inbox(args: dict) -> list[TextContent]:
    """Check SKChat inbox for agent messages."""
    try:
        from skchat.agent_comm import AgentMessenger
    except ImportError:
        return _error_response("skchat not installed. Run: pip install skchat")

    limit = args.get("limit", 20)
    message_type = args.get("message_type")
    identity = _get_skchat_identity()

    messenger = AgentMessenger.from_identity(identity=identity)
    messages = messenger.receive(limit=limit)

    if message_type:
        messages = [m for m in messages if m.get("message_type") == message_type]

    return _json_response({
        "count": len(messages),
        "messages": [
            {
                "message_id": m.get("message_id"),
                "sender": m.get("sender"),
                "content": (m.get("content") or "")[:500],
                "message_type": m.get("message_type", "text"),
                "thread_id": m.get("thread_id"),
                "timestamp": str(m.get("timestamp", "")),
            }
            for m in messages
        ],
    })


async def _handle_skchat_group_create(args: dict) -> list[TextContent]:
    """Create a new SKChat group chat."""
    try:
        from skchat.group import GroupChat
        from skchat.history import ChatHistory
    except ImportError:
        return _error_response("skchat not installed. Run: pip install skchat")

    name = args.get("name", "")
    if not name:
        return _error_response("name is required")

    identity = _get_skchat_identity()
    grp = GroupChat.create(
        name=name,
        creator_uri=identity,
        description=args.get("description", ""),
    )

    # Add initial members if provided
    members_added = []
    for member_uri in args.get("members", []):
        uri = _resolve_recipient(member_uri)
        member = grp.add_member(identity_uri=uri)
        if member:
            members_added.append(uri)

    # Persist the group via ChatHistory
    history = _get_skchat_history()
    thread = grp.to_thread()
    thread.metadata["group_data"] = grp.model_dump(mode="json")
    history.store_thread(thread)

    return _json_response({
        "created": True,
        "group_id": grp.id,
        "name": grp.name,
        "description": grp.description,
        "admin": identity,
        "members": grp.member_uris,
        "members_added": members_added,
        "key_version": grp.key_version,
    })


async def _handle_skchat_group_send(args: dict) -> list[TextContent]:
    """Send a message to an SKChat group."""
    try:
        from skchat.group import GroupChat
        from skchat.models import ChatMessage, ContentType
    except ImportError:
        return _error_response("skchat not installed. Run: pip install skchat")

    group_id = args.get("group_id", "")
    message = args.get("message", "")
    if not group_id or not message:
        return _error_response("group_id and message are required")

    # Load group from storage
    history = _get_skchat_history()
    thread_data = history.get_thread(group_id)
    if thread_data is None:
        return _error_response(f"Group not found: {group_id}")

    group_data = thread_data.get("group_data")
    if group_data is None:
        return _error_response(f"Thread {group_id} is not a group")

    grp = GroupChat.model_validate(group_data)
    identity = _get_skchat_identity()

    msg = ChatMessage(
        sender=identity,
        recipient=f"group:{grp.id}",
        content=message,
        content_type=ContentType.MARKDOWN,
        thread_id=grp.id,
        ttl=args.get("ttl"),
        metadata={"group_message": True, "group_name": grp.name},
    )

    mem_id = history.store_message(msg)

    return _json_response({
        "sent": True,
        "message_id": msg.id,
        "group_id": grp.id,
        "group_name": grp.name,
        "stored": bool(mem_id),
    })


# ═══════════════════════════════════════════════════════════
# Trustee Operations Handlers
# ═══════════════════════════════════════════════════════════


def _get_trustee_ops():
    """Build TrusteeOps and TeamEngine from agent home."""
    from .team_engine import TeamEngine
    from .trustee_ops import TrusteeOps

    home = _home()
    engine = TeamEngine(home=home, provider=None, comms_root=None)
    ops = TrusteeOps(engine=engine, home=home)
    return ops, engine


async def _handle_trustee_health(args: dict) -> list[TextContent]:
    """Run health checks on a deployment."""
    deployment_id = args.get("deployment_id", "")
    if not deployment_id:
        return _error_response("deployment_id is required")

    ops, _ = _get_trustee_ops()
    try:
        report = ops.health_report(deployment_id)
        healthy = sum(1 for r in report if r["healthy"])
        return _json_response({
            "deployment_id": deployment_id,
            "agents": report,
            "summary": {
                "total": len(report),
                "healthy": healthy,
                "degraded": len(report) - healthy,
            },
        })
    except ValueError as exc:
        return _error_response(str(exc))


async def _handle_trustee_restart(args: dict) -> list[TextContent]:
    """Restart agents in a deployment."""
    deployment_id = args.get("deployment_id", "")
    if not deployment_id:
        return _error_response("deployment_id is required")

    agent_name = args.get("agent_name")
    ops, _ = _get_trustee_ops()
    try:
        results = ops.restart_agent(deployment_id, agent_name)
        return _json_response({
            "deployment_id": deployment_id,
            "results": results,
            "all_restarted": all(v == "restarted" for v in results.values()),
        })
    except ValueError as exc:
        return _error_response(str(exc))


async def _handle_trustee_scale(args: dict) -> list[TextContent]:
    """Scale agent instances in a deployment."""
    deployment_id = args.get("deployment_id", "")
    agent_spec_key = args.get("agent_spec_key", "")
    count = args.get("count", 0)
    if not deployment_id or not agent_spec_key or not count:
        return _error_response("deployment_id, agent_spec_key, and count are required")

    ops, _ = _get_trustee_ops()
    try:
        result = ops.scale_agent(deployment_id, agent_spec_key, count)
        return _json_response({
            "deployment_id": deployment_id,
            "agent_spec_key": agent_spec_key,
            **result,
        })
    except ValueError as exc:
        return _error_response(str(exc))


async def _handle_trustee_rotate(args: dict) -> list[TextContent]:
    """Rotate an agent (snapshot + fresh deploy)."""
    deployment_id = args.get("deployment_id", "")
    agent_name = args.get("agent_name", "")
    if not deployment_id or not agent_name:
        return _error_response("deployment_id and agent_name are required")

    ops, _ = _get_trustee_ops()
    try:
        result = ops.rotate_agent(deployment_id, agent_name)
        return _json_response({
            "deployment_id": deployment_id,
            "agent_name": agent_name,
            **result,
        })
    except ValueError as exc:
        return _error_response(str(exc))


async def _handle_trustee_monitor(args: dict) -> list[TextContent]:
    """Run a single monitoring pass."""
    from .trustee_monitor import MonitorConfig, TrusteeMonitor

    ops, engine = _get_trustee_ops()
    config = MonitorConfig(
        heartbeat_timeout=args.get("heartbeat_timeout", 120.0),
        auto_restart=args.get("auto_restart", True),
        auto_rotate=args.get("auto_rotate", True),
    )
    monitor = TrusteeMonitor(ops, engine, config)

    deployment_id = args.get("deployment_id")
    if deployment_id:
        deployment = engine.get_deployment(deployment_id)
        if not deployment:
            return _error_response(f"Deployment '{deployment_id}' not found")
        report = monitor.check_deployment(deployment)
    else:
        report = monitor.check_all()

    return _json_response({
        "timestamp": report.timestamp,
        "deployments_checked": report.deployments_checked,
        "agents_healthy": report.agents_healthy,
        "agents_degraded": report.agents_degraded,
        "restarts_triggered": report.restarts_triggered,
        "rotations_triggered": report.rotations_triggered,
        "escalations_sent": report.escalations_sent,
    })


async def _handle_trustee_logs(args: dict) -> list[TextContent]:
    """Get agent logs from a deployment."""
    deployment_id = args.get("deployment_id", "")
    if not deployment_id:
        return _error_response("deployment_id is required")

    agent_name = args.get("agent_name")
    tail = args.get("tail", 50)
    ops, _ = _get_trustee_ops()
    try:
        logs = ops.get_logs(deployment_id, agent_name, tail=tail)
        return _json_response({
            "deployment_id": deployment_id,
            "agents": {name: lines for name, lines in logs.items()},
        })
    except ValueError as exc:
        return _error_response(str(exc))


async def _handle_trustee_deployments(_args: dict) -> list[TextContent]:
    """List all active deployments."""
    _, engine = _get_trustee_ops()
    deployments = engine.list_deployments()
    return _json_response({
        "count": len(deployments),
        "deployments": [
            {
                "deployment_id": d.deployment_id,
                "blueprint_slug": d.blueprint_slug,
                "team_name": d.team_name,
                "provider": d.provider,
                "status": d.status,
                "agent_count": len(d.agents),
                "agents": {
                    name: {
                        "status": a.status.value if hasattr(a.status, "value") else str(a.status),
                        "host": a.host or "—",
                        "last_heartbeat": a.last_heartbeat or "—",
                    }
                    for name, a in d.agents.items()
                },
            }
            for d in deployments
        ],
    })


# ═══════════════════════════════════════════════════════════
# Heartbeat Handlers
# ═══════════════════════════════════════════════════════════


async def _handle_heartbeat_pulse(args: dict) -> list[TextContent]:
    """Publish a heartbeat beacon."""
    from .heartbeat import HeartbeatBeacon

    home = _home()
    agent_name = _get_agent_name(home)
    beacon = HeartbeatBeacon(home, agent_name=agent_name)
    beacon.initialize()

    hb = beacon.pulse(
        status=args.get("status", "alive"),
        claimed_tasks=args.get("claimed_tasks"),
        loaded_model=args.get("loaded_model", ""),
    )
    return _json_response({
        "agent_name": hb.agent_name,
        "status": hb.status,
        "hostname": hb.hostname,
        "platform": hb.platform,
        "ttl_seconds": hb.ttl_seconds,
        "uptime_hours": hb.uptime_hours,
        "capabilities": [c.name for c in hb.capabilities],
        "fingerprint": hb.fingerprint,
        "capacity": {
            "cpu_count": hb.capacity.cpu_count,
            "memory_total_mb": hb.capacity.memory_total_mb,
            "disk_free_gb": hb.capacity.disk_free_gb,
            "gpu_available": hb.capacity.gpu_available,
        },
    })


async def _handle_heartbeat_peers(args: dict) -> list[TextContent]:
    """Discover peers in the mesh."""
    from .heartbeat import HeartbeatBeacon

    home = _home()
    agent_name = _get_agent_name(home)
    beacon = HeartbeatBeacon(home, agent_name=agent_name)
    beacon.initialize()

    peers = beacon.discover_peers(include_self=args.get("include_self", False))
    return _json_response([
        {
            "agent_name": p.agent_name,
            "status": p.status,
            "alive": p.alive,
            "age_seconds": p.age_seconds,
            "hostname": p.hostname,
            "capabilities": p.capabilities,
            "claimed_tasks": p.claimed_tasks,
        }
        for p in peers
    ])


async def _handle_heartbeat_health(_args: dict) -> list[TextContent]:
    """Get mesh health summary."""
    from .heartbeat import HeartbeatBeacon

    home = _home()
    agent_name = _get_agent_name(home)
    beacon = HeartbeatBeacon(home, agent_name=agent_name)
    beacon.initialize()

    health = beacon.mesh_health()
    return _json_response({
        "total_peers": health.total_peers,
        "alive_peers": health.alive_peers,
        "offline_peers": health.offline_peers,
        "busy_peers": health.busy_peers,
        "total_capabilities": health.total_capabilities,
        "peers": [
            {"agent_name": p.agent_name, "status": p.status, "alive": p.alive}
            for p in health.peers
        ],
    })


async def _handle_heartbeat_find_capable(args: dict) -> list[TextContent]:
    """Find peers with a specific capability."""
    from .heartbeat import HeartbeatBeacon

    home = _home()
    agent_name = _get_agent_name(home)
    beacon = HeartbeatBeacon(home, agent_name=agent_name)
    beacon.initialize()

    capability = args["capability"]
    peers = beacon.find_capable(capability)
    return _json_response({
        "capability": capability,
        "peers": [
            {"agent_name": p.agent_name, "status": p.status, "capabilities": p.capabilities}
            for p in peers
        ],
    })


# ═══════════════════════════════════════════════════════════
# File Transfer Handlers
# ═══════════════════════════════════════════════════════════


async def _handle_file_send(args: dict) -> list[TextContent]:
    """Send a file to another agent."""
    from .file_transfer import FileTransfer

    home = _home()
    agent_name = _get_agent_name(home)
    ft = FileTransfer(home, agent_name=agent_name)
    ft.initialize()

    file_path = Path(args["file_path"])
    manifest = ft.send(
        file_path,
        recipient=args["recipient"],
        encrypt=args.get("encrypt", True),
    )
    return _json_response({
        "transfer_id": manifest.transfer_id,
        "filename": manifest.filename,
        "file_size": manifest.file_size,
        "total_chunks": manifest.total_chunks,
        "sender": manifest.sender,
        "recipient": manifest.recipient,
        "file_sha256": manifest.file_sha256[:16] + "...",
    })


async def _handle_file_receive(args: dict) -> list[TextContent]:
    """Receive and reassemble a file transfer."""
    from .file_transfer import FileTransfer

    home = _home()
    agent_name = _get_agent_name(home)
    ft = FileTransfer(home, agent_name=agent_name)
    ft.initialize()

    output_dir = Path(args["output_dir"]) if args.get("output_dir") else None
    output_path = ft.receive(args["transfer_id"], output_dir=output_dir)
    return _json_response({
        "transfer_id": args["transfer_id"],
        "output_path": str(output_path),
        "file_size": output_path.stat().st_size,
    })


async def _handle_file_list(args: dict) -> list[TextContent]:
    """List file transfers."""
    from .file_transfer import FileTransfer

    home = _home()
    ft = FileTransfer(home, agent_name=_get_agent_name(home))
    ft.initialize()

    transfers = ft.list_transfers(direction=args.get("direction"))
    return _json_response([
        {
            "transfer_id": t.transfer_id,
            "filename": t.filename,
            "file_size": t.file_size,
            "direction": t.direction,
            "progress": round(t.progress, 2),
            "chunks_done": t.chunks_done,
            "total_chunks": t.total_chunks,
            "sender": t.sender,
            "recipient": t.recipient,
        }
        for t in transfers
    ])


async def _handle_file_status(_args: dict) -> list[TextContent]:
    """Get file transfer subsystem status."""
    from .file_transfer import FileTransfer

    home = _home()
    ft = FileTransfer(home, agent_name=_get_agent_name(home))
    ft.initialize()
    return _json_response(ft.status())


# ═══════════════════════════════════════════════════════════
# Pub/Sub Handlers
# ═══════════════════════════════════════════════════════════


async def _handle_pubsub_publish(args: dict) -> list[TextContent]:
    """Publish a message to a topic."""
    from .pubsub import PubSub

    home = _home()
    agent_name = _get_agent_name(home)
    ps = PubSub(home, agent_name=agent_name)
    ps.initialize()

    msg = ps.publish(
        topic=args["topic"],
        payload=args["payload"],
        ttl_seconds=args.get("ttl_seconds", 3600),
    )
    return _json_response({
        "message_id": msg.message_id,
        "topic": msg.topic,
        "sender": msg.sender,
        "published_at": str(msg.published_at),
    })


async def _handle_pubsub_subscribe(args: dict) -> list[TextContent]:
    """Subscribe to a topic pattern."""
    from .pubsub import PubSub

    home = _home()
    agent_name = _get_agent_name(home)
    ps = PubSub(home, agent_name=agent_name)
    ps.initialize()

    sub = ps.subscribe(args["pattern"])
    return _json_response({
        "pattern": sub.pattern,
        "agent": agent_name,
        "subscribed_at": str(sub.subscribed_at),
    })


async def _handle_pubsub_poll(args: dict) -> list[TextContent]:
    """Poll for new messages."""
    from .pubsub import PubSub

    home = _home()
    agent_name = _get_agent_name(home)
    ps = PubSub(home, agent_name=agent_name)
    ps.initialize()

    messages = ps.poll(
        topic=args.get("topic"),
        limit=args.get("limit", 50),
    )
    return _json_response([
        {
            "message_id": m.message_id,
            "topic": m.topic,
            "sender": m.sender,
            "payload": m.payload,
            "published_at": str(m.published_at),
        }
        for m in messages
    ])


async def _handle_pubsub_topics(_args: dict) -> list[TextContent]:
    """List all known topics."""
    from .pubsub import PubSub

    home = _home()
    ps = PubSub(home, agent_name=_get_agent_name(home))
    ps.initialize()
    return _json_response(ps.list_topics())


# ═══════════════════════════════════════════════════════════
# Memory Fortress Handlers
# ═══════════════════════════════════════════════════════════


async def _handle_fortress_verify(args: dict) -> list[TextContent]:
    """Verify memory integrity."""
    from .memory_fortress import MemoryFortress

    home = _home()
    fortress = MemoryFortress(home)
    fortress.initialize()

    layer = args.get("layer")
    if layer:
        layer_dir = home / "memory" / layer
        if not layer_dir.is_dir():
            return _error_response(f"Layer directory not found: {layer}")
        results = []
        for f in sorted(layer_dir.glob("*.json")):
            _, seal_result = fortress.verify_and_load(f)
            results.append({
                "memory_id": seal_result.memory_id,
                "verified": seal_result.verified,
                "tampered": seal_result.tampered,
                "sealed": seal_result.sealed,
            })
    else:
        seal_results = fortress.verify_all(home)
        results = [
            {
                "memory_id": r.memory_id,
                "verified": r.verified,
                "tampered": r.tampered,
                "sealed": r.sealed,
            }
            for r in seal_results
        ]

    tampered = sum(1 for r in results if r.get("tampered"))
    verified = sum(1 for r in results if r.get("verified"))
    return _json_response({
        "total": len(results),
        "verified": verified,
        "tampered": tampered,
        "unsealed": len(results) - verified - tampered,
        "details": results,
    })


async def _handle_fortress_seal_existing(_args: dict) -> list[TextContent]:
    """Seal all unsealed memories."""
    from .memory_fortress import MemoryFortress

    home = _home()
    fortress = MemoryFortress(home)
    fortress.initialize()

    sealed_count = fortress.seal_existing(home)
    return _json_response({
        "sealed": sealed_count,
        "message": f"Sealed {sealed_count} previously unsealed memories",
    })


async def _handle_fortress_status(_args: dict) -> list[TextContent]:
    """Get Memory Fortress status."""
    from .memory_fortress import MemoryFortress

    home = _home()
    fortress = MemoryFortress(home)
    fortress.initialize()
    return _json_response(fortress.status())


# ═══════════════════════════════════════════════════════════
# Memory Promoter Handlers
# ═══════════════════════════════════════════════════════════


async def _handle_promoter_sweep(args: dict) -> list[TextContent]:
    """Run a memory promotion sweep."""
    from .memory_promoter import PromotionEngine

    home = _home()
    engine = PromotionEngine(home)

    result = engine.sweep(
        dry_run=args.get("dry_run", False),
        limit=args.get("limit"),
        layer=args.get("layer"),
    )
    return _json_response({
        "scanned": result.scanned,
        "promoted": result.promoted,
        "skipped": result.skipped,
        "dry_run": result.dry_run,
        "by_layer": result.by_layer,
        "promotions": [
            {
                "memory_id": c.memory_id,
                "current_layer": c.current_layer,
                "target_layer": c.target_layer,
                "score": round(c.score, 3),
                "promoted": c.promoted,
            }
            for c in result.candidates
            if c.promoted or result.dry_run
        ],
    })


async def _handle_promoter_history(args: dict) -> list[TextContent]:
    """View promotion history."""
    from .memory_promoter import PromotionEngine

    home = _home()
    engine = PromotionEngine(home)
    history = engine.get_history(limit=args.get("limit", 20))
    return _json_response(history)


# ═══════════════════════════════════════════════════════════
# KMS Handlers
# ═══════════════════════════════════════════════════════════


async def _handle_kms_status(_args: dict) -> list[TextContent]:
    """Get KMS status."""
    from .kms import KeyStore

    home = _home()
    store = KeyStore(home)
    store.initialize()
    return _json_response(store.status())


async def _handle_kms_list_keys(args: dict) -> list[TextContent]:
    """List all KMS keys."""
    from .kms import KeyStore

    home = _home()
    store = KeyStore(home)
    store.initialize()

    key_type = args.get("key_type")
    include_revoked = args.get("include_revoked", False)
    keys = store.list_keys(key_type=key_type, include_revoked=include_revoked)
    return _json_response([
        {
            "key_id": k.key_id,
            "key_type": k.key_type,
            "status": k.status,
            "label": k.label,
            "created_at": str(k.created_at),
            "version": k.version,
            "rotation_count": k.rotation_count,
        }
        for k in keys
    ])


async def _handle_kms_rotate(args: dict) -> list[TextContent]:
    """Rotate a KMS key."""
    from .kms import KeyStore

    home = _home()
    store = KeyStore(home)
    store.initialize()

    new_key = store.rotate_key(
        key_id=args["key_id"],
        reason=args.get("reason", "scheduled"),
    )
    return _json_response({
        "key_id": new_key.key_id,
        "version": new_key.version,
        "status": new_key.status,
        "rotation_count": new_key.rotation_count,
        "message": f"Key rotated to version {new_key.version}",
    })


# ═══════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════


def main() -> None:
    """Run the MCP server on stdio transport."""
    logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")
    asyncio.run(_run_server())


async def _run_server() -> None:
    """Async entry point for the stdio MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    main()
