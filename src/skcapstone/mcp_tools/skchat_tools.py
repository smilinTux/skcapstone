"""SKChat send and history tools.

Exposes two tools:
    chat_send    — Send a message via SKChat
    chat_history — Retrieve chat history
"""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _home, _json_response

TOOLS: list[Tool] = [
    Tool(
        name="chat_send",
        description=(
            "Send a chat message to another agent via SKChat. "
            "Wraps the AgentMessenger for delivery with optional "
            "threading, structured payloads, and ephemeral (TTL) support."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "recipient": {
                    "type": "string",
                    "description": "Recipient agent name or CapAuth URI",
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
            },
            "required": ["recipient", "message"],
        },
    ),
    Tool(
        name="chat_history",
        description=(
            "Retrieve chat history from SKChat. Returns recent messages "
            "with sender, content, type, thread, and timestamp. "
            "Optionally filter by peer or thread."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "peer": {
                    "type": "string",
                    "description": "Filter by peer agent name or URI",
                },
                "thread_id": {
                    "type": "string",
                    "description": "Filter by thread/conversation ID",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum messages to return (default: 20)",
                },
            },
            "required": [],
        },
    ),
]


# ── Helpers ──────────────────────────────────────────────────


def _get_skchat_identity() -> str:
    """Resolve the sovereign identity for SKChat operations."""
    try:
        from skchat.identity_bridge import get_sovereign_identity  # type: ignore[import]
        return get_sovereign_identity()
    except ImportError:
        from ..runtime import get_runtime
        home = _home()
        runtime = get_runtime(home)
        return f"capauth:{runtime.manifest.name}@local"
    except Exception:
        return "capauth:agent@local"


def _resolve_recipient(name: str) -> str:
    """Resolve a short agent name to a CapAuth URI if needed."""
    if ":" in name:
        return name
    try:
        from skchat.identity_bridge import resolve_peer_name  # type: ignore[import]
        return resolve_peer_name(name)
    except Exception:
        return f"capauth:{name}@local"


# ── Handlers ─────────────────────────────────────────────────


async def _handle_chat_send(args: dict) -> list[TextContent]:
    """Send a chat message via SKChat AgentMessenger."""
    try:
        from skchat.agent_comm import AgentMessenger  # type: ignore[import]
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
    )

    return _json_response({
        "sent": True,
        "message_id": result.get("message_id"),
        "recipient": recipient_uri,
        "delivered": result.get("delivered", False),
        "transport": result.get("transport"),
        "error": result.get("error"),
    })


async def _handle_chat_history(args: dict) -> list[TextContent]:
    """Retrieve chat history from SKChat."""
    try:
        from skchat.history import ChatHistory  # type: ignore[import]
    except ImportError:
        return _error_response("skchat not installed. Run: pip install skchat")

    limit = args.get("limit", 20)
    peer = args.get("peer")
    thread_id = args.get("thread_id")

    try:
        history = ChatHistory.from_config()
        messages = history.recent(limit=limit, peer=peer, thread_id=thread_id)
        return _json_response({
            "count": len(messages),
            "messages": [
                {
                    "message_id": m.get("message_id") or m.get("id"),
                    "sender": m.get("sender"),
                    "recipient": m.get("recipient"),
                    "content": (m.get("content") or "")[:500],
                    "message_type": m.get("message_type", "text"),
                    "thread_id": m.get("thread_id"),
                    "timestamp": str(m.get("timestamp", "")),
                }
                for m in messages
            ],
        })
    except Exception as exc:
        return _error_response(f"Could not retrieve chat history: {exc}")


HANDLERS: dict = {
    "chat_send": _handle_chat_send,
    "chat_history": _handle_chat_history,
}
