"""Desktop notification tool — send_notification via notify-send."""

from __future__ import annotations

import asyncio
import datetime

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _home, _json_response

TOOLS: list[Tool] = [
    Tool(
        name="send_notification",
        description=(
            "Send a desktop notification via notify-send. "
            "Stores the event in agent memory with tag=notification "
            "and returns {sent, timestamp}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Notification title",
                },
                "body": {
                    "type": "string",
                    "description": "Notification body text",
                },
                "urgency": {
                    "type": "string",
                    "enum": ["low", "normal", "critical"],
                    "description": "Urgency level: low, normal, or critical (default: normal)",
                },
            },
            "required": ["title", "body"],
        },
    ),
]


async def _handle_send_notification(args: dict) -> list[TextContent]:
    """Send a desktop notification and store memory record."""
    title = args.get("title", "").strip()
    body = args.get("body", "").strip()
    urgency = args.get("urgency", "normal")

    if not title:
        return _error_response("title is required")
    if not body:
        return _error_response("body is required")
    if urgency not in {"low", "normal", "critical"}:
        return _error_response("urgency must be one of: low, normal, critical")

    # Run notify-send in a subprocess (non-blocking).
    proc = await asyncio.create_subprocess_exec(
        "notify-send",
        "--urgency", urgency,
        title,
        body,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        err_msg = stderr.decode(errors="replace").strip() if stderr else "unknown error"
        return _error_response(f"notify-send failed (exit {proc.returncode}): {err_msg}")

    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Persist a memory entry so the agent recalls past notifications.
    try:
        from ..memory_engine import store as memory_store

        memory_store(
            home=_home(),
            content=f"Notification sent — title: {title!r}, body: {body!r}, urgency: {urgency}",
            tags=["notification"],
            source="mcp:send_notification",
            importance=0.4,
        )
    except Exception:
        pass  # memory failure must not block the notification response

    return _json_response({"sent": True, "timestamp": timestamp})


HANDLERS: dict = {
    "send_notification": _handle_send_notification,
}
