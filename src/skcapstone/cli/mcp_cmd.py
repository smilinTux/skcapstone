"""MCP (Model Context Protocol) server commands: serve."""

from __future__ import annotations

import click


def register_mcp_commands(main: click.Group) -> None:
    """Register the mcp command group."""

    @main.group()
    def mcp():
        """MCP (Model Context Protocol) server.

        Expose sovereign agent capabilities as MCP tools for
        AI platforms like Cursor and Claude Desktop.
        """

    @mcp.command("serve")
    def mcp_serve():
        """Start the MCP server on stdio transport.

        Exposes agent_status, memory_recall, memory_store, send_message,
        check_inbox, sync_push, sync_pull, coord_status, coord_claim,
        and coord_complete as MCP tools.

        For Cursor: configure in .cursor/mcp.json.
        For Claude Desktop: add to claude_desktop_config.json.
        """
        from ..mcp_server import main as mcp_main

        mcp_main()
