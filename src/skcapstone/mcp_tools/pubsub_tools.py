"""Pub/sub messaging tools."""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _get_agent_name, _home, _json_response, _shared_root

TOOLS: list[Tool] = [
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
]


async def _handle_pubsub_publish(args: dict) -> list[TextContent]:
    """Publish a message to a topic."""
    from ..pubsub import PubSub

    home = _home()
    agent_name = _get_agent_name(home)
    ps = PubSub(_shared_root(), agent_name=agent_name)
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
    from ..pubsub import PubSub

    home = _home()
    agent_name = _get_agent_name(home)
    ps = PubSub(_shared_root(), agent_name=agent_name)
    ps.initialize()

    sub = ps.subscribe(args["pattern"])
    return _json_response({
        "pattern": sub.pattern,
        "agent": agent_name,
        "subscribed_at": str(sub.subscribed_at),
    })


async def _handle_pubsub_poll(args: dict) -> list[TextContent]:
    """Poll for new messages."""
    from ..pubsub import PubSub

    home = _home()
    agent_name = _get_agent_name(home)
    ps = PubSub(_shared_root(), agent_name=agent_name)
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
    from ..pubsub import PubSub

    home = _home()
    ps = PubSub(_shared_root(), agent_name=_get_agent_name(home))
    ps.initialize()
    return _json_response(ps.list_topics())


HANDLERS: dict = {
    "pubsub_publish": _handle_pubsub_publish,
    "pubsub_subscribe": _handle_pubsub_subscribe,
    "pubsub_poll": _handle_pubsub_poll,
    "pubsub_topics": _handle_pubsub_topics,
}
