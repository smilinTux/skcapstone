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
    model_route     — Route task to optimal model tier/name
    send_notification — Send desktop notification via notify-send

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
import logging

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .mcp_tools import collect_all_handlers, collect_all_tools

# Re-export helpers so existing tests and imports keep working.
from .mcp_tools._helpers import (  # noqa: F401
    _error_response,
    _get_agent_name,
    _home,
    _json_response,
    _text_response,
)

logger = logging.getLogger("skcapstone.mcp")

server = Server("skcapstone")


# ═══════════════════════════════════════════════════════════
# Tool Definitions (delegated to mcp_tools subpackage)
# ═══════════════════════════════════════════════════════════


@server.list_tools()
async def list_tools() -> list[Tool]:
    """Register all skcapstone tools with the MCP server."""
    return collect_all_tools()


# ═══════════════════════════════════════════════════════════
# Tool Dispatch (delegated to mcp_tools subpackage)
# ═══════════════════════════════════════════════════════════

# Build handler table once at import time.
_ALL_HANDLERS = collect_all_handlers()


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch tool calls to the appropriate handler."""
    handler = _ALL_HANDLERS.get(name)
    if handler is None:
        return _error_response(f"Unknown tool: {name}")
    try:
        return await handler(arguments)
    except Exception as exc:
        logger.exception("Tool '%s' failed", name)
        return _error_response(f"{name} failed: {exc}")


# ═══════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════


def main() -> None:
    """Run the MCP server on stdio transport."""
    logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")
    asyncio.run(_run_server())


async def _run_server() -> None:
    """Async entry point for the stdio MCP server."""
    from .mcp_tools._helpers import _get_agent_name, _home, _shared_root
    from .pubsub import PubSub

    home = _home()
    ps = PubSub(_shared_root(), agent_name=_get_agent_name(home))
    ps.initialize()
    expiry_task = asyncio.create_task(
        ps.start_expiry_task(interval=300),
        name="pubsub-expiry",
    )
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        expiry_task.cancel()
        try:
            await expiry_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    main()
