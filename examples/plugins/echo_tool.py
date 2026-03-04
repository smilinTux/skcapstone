"""Echo tool plugin — SKCapstone plugin example.

Adds a single MCP tool ``echo`` that returns the caller's message verbatim.
Use this as a minimal starting point for writing your own plugins.

Installation
------------
Copy (or symlink) this file into your plugin directory::

    cp skcapstone/examples/plugins/echo_tool.py ~/.skcapstone/plugins/

Then either restart the MCP server or send SIGHUP to the daemon to hot-reload::

    kill -HUP $(cat ~/.skcapstone/daemon.pid)

Verify the tool is available::

    skcapstone-mcp   # start MCP server and list tools — "echo" should appear

Plugin contract
---------------
Every plugin module must expose exactly one function::

    def register(mcp_server, app) -> None: ...

``mcp_server``
    The :class:`mcp.server.Server` instance when loaded by the MCP server
    process, or ``None`` when loaded by the daemon process.

``app``
    The FastAPI :class:`~fastapi.FastAPI` instance when the docs server is
    running, or ``None`` otherwise.

Call :func:`skcapstone.plugins.register_tool` inside ``register()`` to add
MCP tools.  The function is idempotent — re-registering the same name
replaces the previous entry.
"""

from mcp.types import TextContent, Tool

from skcapstone.plugins import register_tool


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


async def _echo_handler(arguments: dict) -> list[TextContent]:
    """Return the caller's message unchanged."""
    message = str(arguments.get("message", ""))
    return [TextContent(type="text", text=f"echo: {message}")]


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(mcp_server, app) -> None:  # noqa: ARG001
    """Register the ``echo`` MCP tool with the plugin registry.

    ``mcp_server`` and ``app`` are accepted for API compatibility but are not
    used here — tool registration goes through the module-level
    ``_registry`` singleton so it works in both the daemon and MCP server
    processes.
    """
    register_tool(
        Tool(
            name="echo",
            description=(
                "Echo back the given message unchanged. "
                "Useful for testing the plugin system."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The message to echo back.",
                    },
                },
                "required": ["message"],
            },
        ),
        _echo_handler,
    )
