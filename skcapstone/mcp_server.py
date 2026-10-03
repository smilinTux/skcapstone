"""
SKCapstone MCP Server - sovereign agent capabilities via Model Context Protocol.

Tool-agnostic: works with Cursor, Claude Code CLI, Claude Desktop,
Windsurf, Aider, Cline, or any MCP client that speaks stdio.

This module is a thin registration + bootstrap layer. Every tool's schema and
handler lives in a domain module under ``skcapstone.mcp_tools`` (each exposing
``TOOLS``, ``HANDLERS`` and an optional ``HIDDEN`` set). ``list_tools`` and
``call_tool`` simply aggregate across those modules via
``mcp_tools.collect_all_tools`` / ``collect_all_handlers``. To add or change a
tool, edit its domain module - never redefine schemas here.

Invocation (all equivalent):
    skcapstone mcp serve                     # CLI entry point
    python -m skcapstone.mcp_server          # direct module
    bash skcapstone/scripts/mcp-serve.sh     # portable launcher

Client configuration - use the launcher script for all clients:

    Cursor (.cursor/mcp.json) / Claude Code CLI (.mcp.json) / Claude Desktop:
        {"mcpServers": {"skcapstone": {
            "command": "bash", "args": ["skcapstone/scripts/mcp-serve.sh"]}}}

        Or interactively: claude mcp add skcapstone -- bash skcapstone/scripts/mcp-serve.sh

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

from . import mcp_tools
from .mcp_tools._helpers import (
    _error_response,
    _get_agent_name,
    _home,
    _json_response,
    _text_response,
)

# Re-exported for backward compatibility with modules/tests that import these
# helpers (and TOOLS/server) from ``skcapstone.mcp_server``.
__all__ = [
    "server",
    "TOOLS",
    "list_tools",
    "call_tool",
    "main",
    "_run_server",
    "_home",
    "_json_response",
    "_error_response",
    "_text_response",
    "_get_agent_name",
]

logger = logging.getLogger("skcapstone.mcp")

server = Server("skcapstone")

# Snapshot of the published tool surface, built once from the domain modules.
# Consumers (e.g. context_loader) import this for a quick tool count.
TOOLS: list[Tool] = mcp_tools.collect_all_tools()


@server.list_tools()
async def list_tools() -> list[Tool]:
    """Return every published MCP tool, aggregated from the domain modules."""
    return mcp_tools.collect_all_tools()


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch a tool call to the handler registered by its domain module."""
    handler = mcp_tools.collect_all_handlers().get(name)
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
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    main()
