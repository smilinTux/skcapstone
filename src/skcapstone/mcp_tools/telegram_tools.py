"""Telegram integration tools — import, send, poll, and manage chats."""

from __future__ import annotations

import asyncio
import json

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _json_response

# ═══════════════════════════════════════════════════════════
# Tool Definitions
# ═══════════════════════════════════════════════════════════

TOOLS: list[Tool] = [
    Tool(
        name="telegram_import",
        description=(
            "Import a Telegram Desktop chat export into memories. "
            "Point to the export directory containing result.json."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "export_path": {
                    "type": "string",
                    "description": "Path to Telegram export directory or result.json file",
                },
                "mode": {
                    "type": "string",
                    "description": "Import mode: 'daily' (consolidate per day) or 'message' (one per message)",
                    "enum": ["daily", "message"],
                    "default": "daily",
                },
                "min_length": {
                    "type": "integer",
                    "description": "Skip messages shorter than this many characters",
                    "default": 30,
                },
                "chat_name": {
                    "type": "string",
                    "description": "Override the chat name from the export",
                },
                "tags": {
                    "type": "string",
                    "description": "Extra comma-separated tags to apply",
                },
            },
            "required": ["export_path"],
        },
    ),
    Tool(
        name="telegram_import_api",
        description=(
            "Import messages directly from Telegram API using Telethon. "
            "Requires TELEGRAM_API_ID and TELEGRAM_API_HASH env vars. "
            "No manual export needed — connects and pulls messages directly."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat": {
                    "type": "string",
                    "description": "Chat username, title, or numeric ID to import from",
                },
                "mode": {
                    "type": "string",
                    "description": "Import mode: 'daily' or 'message'",
                    "enum": ["daily", "message"],
                    "default": "daily",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of messages to fetch",
                },
                "since": {
                    "type": "string",
                    "description": "Only fetch messages after this date (YYYY-MM-DD)",
                },
                "min_length": {
                    "type": "integer",
                    "description": "Skip messages shorter than this many characters",
                    "default": 30,
                },
                "chat_name": {
                    "type": "string",
                    "description": "Override the chat name",
                },
                "tags": {
                    "type": "string",
                    "description": "Extra comma-separated tags",
                },
            },
            "required": ["chat"],
        },
    ),
    Tool(
        name="telegram_setup",
        description=(
            "Check Telegram API setup status. Reports whether Telethon is "
            "installed, API credentials are set, and a session file exists."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="telegram_send",
        description=(
            "Send a message to a Telegram chat via Telethon. "
            "Requires TELEGRAM_API_ID and TELEGRAM_API_HASH env vars."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat": {
                    "type": "string",
                    "description": "Chat username, title, or numeric ID",
                },
                "message": {
                    "type": "string",
                    "description": "Message text to send",
                },
                "parse_mode": {
                    "type": "string",
                    "enum": ["html", "markdown"],
                    "description": "Optional parse mode for message formatting",
                },
            },
            "required": ["chat", "message"],
        },
    ),
    Tool(
        name="telegram_poll",
        description=(
            "Fetch recent messages from a Telegram chat (one-shot poll). "
            "Returns messages as a JSON array. Requires Telethon API credentials."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat": {
                    "type": "string",
                    "description": "Chat username, title, or numeric ID",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of messages to fetch (default: 20)",
                    "default": 20,
                },
                "since": {
                    "type": "string",
                    "description": "Only fetch messages after this ISO date (YYYY-MM-DD)",
                },
            },
            "required": ["chat"],
        },
    ),
    Tool(
        name="telegram_catchup",
        description=(
            "Full catch-up import from a Telegram group into ALL memory tiers. "
            "Downloads chat via Telethon and distributes: last 24h → short-term "
            "(individual messages), last 7 days → mid-term (daily summaries), "
            "older → long-term (weekly summaries). Use this to rehydrate an "
            "agent's context from a Telegram group."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat": {
                    "type": "string",
                    "description": "Chat username, title, or numeric ID to catch up from",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum total messages to fetch (default: 2000)",
                    "default": 2000,
                },
                "since": {
                    "type": "string",
                    "description": "Only fetch messages after this date (YYYY-MM-DD)",
                },
                "min_length": {
                    "type": "integer",
                    "description": "Skip messages shorter than this (default: 20)",
                    "default": 20,
                },
                "tags": {
                    "type": "string",
                    "description": "Extra comma-separated tags to apply",
                },
            },
            "required": ["chat"],
        },
    ),
    Tool(
        name="telegram_chats",
        description=(
            "List available Telegram chats, groups, and channels. "
            "Returns id, title, type, and unread count for each."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of chats to list (default: 50)",
                    "default": 50,
                },
            },
            "required": [],
        },
    ),
]


# ═══════════════════════════════════════════════════════════
# Handlers
# ═══════════════════════════════════════════════════════════


async def _handle_telegram_import(args: dict) -> list[TextContent]:
    """Import a Telegram Desktop chat export into memories."""
    try:
        from skmemory.importers.telegram import import_telegram
        from skmemory.store import MemoryStore

        export_path = args["export_path"]
        mode = args.get("mode", "daily")
        min_length = args.get("min_length", 30)
        chat_name = args.get("chat_name")
        tags_str = args.get("tags", "")
        tags = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else None

        store = MemoryStore()
        stats = import_telegram(
            store,
            export_path,
            mode=mode,
            min_message_length=min_length,
            chat_name=chat_name,
            tags=tags,
        )
        return _json_response(stats)
    except Exception as e:
        return _json_response({"error": str(e)})


async def _handle_telegram_import_api(args: dict) -> list[TextContent]:
    """Import messages directly from Telegram API."""
    try:
        from skmemory.importers.telegram_api import import_telegram_api
        from skmemory.store import MemoryStore

        chat = args["chat"]
        mode = args.get("mode", "daily")
        limit = args.get("limit")
        since = args.get("since")
        min_length = args.get("min_length", 30)
        chat_name = args.get("chat_name")
        tags_str = args.get("tags", "")
        tags = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else None

        store = MemoryStore()
        stats = import_telegram_api(
            store,
            chat,
            mode=mode,
            limit=limit,
            since=since,
            min_message_length=min_length,
            chat_name=chat_name,
            tags=tags,
        )
        return _json_response(stats)
    except Exception as e:
        return _json_response({"error": str(e)})


async def _handle_telegram_setup(args: dict) -> list[TextContent]:
    """Check Telegram API setup status."""
    try:
        from skmemory.importers.telegram_api import check_setup

        result = check_setup()
        return _json_response(result)
    except ImportError:
        return _json_response({
            "ready": False,
            "error": "skmemory package not available",
            "messages": ["Install skmemory: pip install skmemory[telegram]"],
        })
    except Exception as e:
        return _json_response({"error": str(e)})


async def _handle_telegram_send(args: dict) -> list[TextContent]:
    """Send a message to a Telegram chat."""
    try:
        from skmemory.importers.telegram_api import send_message

        chat = args["chat"]
        message = args["message"]
        parse_mode = args.get("parse_mode")

        result = asyncio.get_event_loop().run_in_executor(
            None, lambda: asyncio.run(send_message(chat, message, parse_mode))
        )
        # Since we're already in an async context, run the coroutine directly
        # but we need a new event loop because Telethon creates its own
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = await asyncio.get_event_loop().run_in_executor(
                pool,
                lambda: asyncio.run(send_message(chat, message, parse_mode)),
            )

        return _json_response(result)
    except Exception as e:
        return _json_response({"error": str(e)})


async def _handle_telegram_poll(args: dict) -> list[TextContent]:
    """Fetch recent messages from a Telegram chat."""
    try:
        from skmemory.importers.telegram_api import poll_messages

        chat = args["chat"]
        limit = args.get("limit", 20)
        since = args.get("since")

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            messages = await asyncio.get_event_loop().run_in_executor(
                pool,
                lambda: asyncio.run(poll_messages(chat, limit=limit, since=since)),
            )

        return _json_response({
            "chat": chat,
            "count": len(messages),
            "messages": messages,
        })
    except Exception as e:
        return _json_response({"error": str(e)})


async def _handle_telegram_catchup(args: dict) -> list[TextContent]:
    """Full catch-up import from Telegram into all memory tiers."""
    try:
        from skmemory.importers.telegram_api import import_telegram_api
        from skmemory.store import MemoryStore

        chat = args["chat"]
        limit = args.get("limit", 2000)
        since = args.get("since")
        min_length = args.get("min_length", 20)
        tags_str = args.get("tags", "")
        tags = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else None

        store = MemoryStore()
        stats = import_telegram_api(
            store,
            chat,
            mode="catchup",
            limit=limit,
            since=since,
            min_message_length=min_length,
            tags=tags,
        )
        return _json_response(stats)
    except Exception as e:
        return _json_response({"error": str(e)})


async def _handle_telegram_chats(args: dict) -> list[TextContent]:
    """List available Telegram chats."""
    try:
        from skmemory.importers.telegram_api import list_chats

        limit = args.get("limit", 50)

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            chats = await asyncio.get_event_loop().run_in_executor(
                pool,
                lambda: asyncio.run(list_chats(limit=limit)),
            )

        return _json_response({
            "count": len(chats),
            "chats": chats,
        })
    except Exception as e:
        return _json_response({"error": str(e)})


HANDLERS: dict = {
    "telegram_import": _handle_telegram_import,
    "telegram_import_api": _handle_telegram_import_api,
    "telegram_setup": _handle_telegram_setup,
    "telegram_send": _handle_telegram_send,
    "telegram_poll": _handle_telegram_poll,
    "telegram_catchup": _handle_telegram_catchup,
    "telegram_chats": _handle_telegram_chats,
}
