"""Desktop notification tool — send_notification via notify-send."""

from __future__ import annotations

import asyncio
import datetime
import os

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

    # Log notification to skcomm/notifications/ (not memory/).
    try:
        import json as _j
        import uuid
        home = _home()
        agent_name = os.environ.get("SKCAPSTONE_AGENT", "lumina")
        notif_dir = home / "agents" / agent_name / "skcomm" / "notifications"
        notif_dir.mkdir(parents=True, exist_ok=True)
        entry = {"id": uuid.uuid4().hex[:12], "type": "notification-sent",
                 "title": title, "body": body, "urgency": urgency, "timestamp": timestamp}
        (notif_dir / f"{entry['id']}.json").write_text(_j.dumps(entry, indent=2))
    except Exception:
        pass  # notification log failure must not block the response

    return _json_response({"sent": True, "timestamp": timestamp})


HANDLERS: dict = {
    "send_notification": _handle_send_notification,
}
