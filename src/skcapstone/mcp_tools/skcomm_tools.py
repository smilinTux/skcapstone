"""SKComm notification and status tools.

Exposes two tools:
    comm_notify — Send a notification via SKComm transports
    comm_status — Show SKComm subsystem status
"""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _home, _json_response

TOOLS: list[Tool] = [
    Tool(
        name="comm_notify",
        description=(
            "Send a notification message via SKComm. Routes through "
            "available transports (Syncthing, file, Tailscale). "
            "Supports urgency levels for priority routing."
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
                    "description": "Notification message content",
                },
                "urgency": {
                    "type": "string",
                    "enum": ["low", "normal", "high", "critical"],
                    "description": "Notification urgency (default: normal)",
                },
                "subject": {
                    "type": "string",
                    "description": "Optional notification subject line",
                },
            },
            "required": ["recipient", "message"],
        },
    ),
    Tool(
        name="comm_status",
        description=(
            "Show SKComm subsystem status: installed version, "
            "available transports, connection state, and recent "
            "delivery statistics."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
]


async def _handle_comm_notify(args: dict) -> list[TextContent]:
    """Send a notification via SKComm."""
    recipient = args.get("recipient", "")
    message = args.get("message", "")
    if not recipient or not message:
        return _error_response("recipient and message are required")

    try:
        from skcomm.core import SKComm  # type: ignore[import]

        comm = SKComm.from_config()
        report = comm.send(recipient, message)
        return _json_response({
            "sent": report.success,
            "recipient": recipient,
            "urgency": args.get("urgency", "normal"),
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
        return _error_response(f"Notification send failed: {exc}")


async def _handle_comm_status(_args: dict) -> list[TextContent]:
    """Show SKComm subsystem status."""
    result: dict = {}

    try:
        import skcomm  # type: ignore[import]

        result["installed"] = True
        result["version"] = getattr(skcomm, "__version__", "unknown")
    except ImportError:
        result["installed"] = False
        result["version"] = None
        return _json_response(result)

    try:
        from skcomm.core import SKComm  # type: ignore[import]

        comm = SKComm.from_config()
        result["transports"] = [
            t.name for t in getattr(comm, "transports", [])
        ]
        result["connected"] = True
    except Exception as exc:
        result["transports"] = []
        result["connected"] = False
        result["error"] = str(exc)

    return _json_response(result)


HANDLERS: dict = {
    "comm_notify": _handle_comm_notify,
    "comm_status": _handle_comm_status,
}
