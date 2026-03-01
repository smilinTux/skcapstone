"""SKChat messaging tools."""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _home, _json_response

TOOLS: list[Tool] = [
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
]


# ── Helpers ──────────────────────────────────────────────────


def _get_skchat_identity() -> str:
    """Resolve the sovereign identity for SKChat operations."""
    try:
        from skchat.identity_bridge import get_sovereign_identity
        return get_sovereign_identity()
    except ImportError:
        from ..runtime import get_runtime
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


# ── Handlers ─────────────────────────────────────────────────


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


HANDLERS: dict = {
    "skchat_send": _handle_skchat_send,
    "skchat_inbox": _handle_skchat_inbox,
    "skchat_group_create": _handle_skchat_group_create,
    "skchat_group_send": _handle_skchat_group_send,
}
