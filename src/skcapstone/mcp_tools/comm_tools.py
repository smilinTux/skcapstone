"""SKComm send/receive messaging tools."""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _json_response

TOOLS: list[Tool] = [
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
]


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


HANDLERS: dict = {
    "send_message": _handle_send_message,
    "check_inbox": _handle_check_inbox,
}
