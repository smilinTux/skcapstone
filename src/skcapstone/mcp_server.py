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
