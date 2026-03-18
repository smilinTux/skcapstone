"""MCP tool group modules — split from mcp_server.py for maintainability.

Each module exposes:
    TOOLS:    list[Tool]   — MCP tool definitions
    HANDLERS: dict         — {tool_name: async_handler_fn}

The ``collect_all_tools`` and ``collect_all_handlers`` functions aggregate
across every module so mcp_server.py can register them in one shot.
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine

from mcp.types import TextContent, Tool

from . import (
    agent_tools,
    ansible_tools,
    brain_first_tools,
    chat_tools,
    comm_tools,
    consciousness_tools,
    coord_tools,
    deploy_tools,
    did_tools,
    emotion_tools,
    file_tools,
    fortress_tools,
    gtd_tools,
    health_tools,
    heartbeat_tools,
    itil_tools,
    kms_tools,
    memory_tools,
    model_tools,
    notification_tools,
    promoter_tools,
    pubsub_tools,
    skills_tools,
    skseed_tools,
    skstacks_tools,
    soul_tools,
    sync_tools,
    telegram_tools,
    trust_tools,
    trustee_tools,
)

# Ordered list of all tool-group modules.
_MODULES = [
    agent_tools,
    brain_first_tools,
    memory_tools,
    comm_tools,
    sync_tools,
    coord_tools,
    ansible_tools,
    soul_tools,
    did_tools,
    trust_tools,
    skills_tools,
    chat_tools,
    trustee_tools,
    health_tools,
    heartbeat_tools,
    file_tools,
    gtd_tools,
    itil_tools,
    pubsub_tools,
    fortress_tools,
    promoter_tools,
    kms_tools,
    skseed_tools,
    skstacks_tools,
    deploy_tools,
    model_tools,
    consciousness_tools,
    emotion_tools,
    notification_tools,
    telegram_tools,
]


def collect_all_tools() -> list[Tool]:
    """Return every Tool definition from all group modules."""
    tools: list[Tool] = []
    for mod in _MODULES:
        tools.extend(mod.TOOLS)
    return tools


def collect_all_handlers() -> dict[str, Callable[..., Coroutine[Any, Any, list[TextContent]]]]:
    """Return a merged {name: handler} dict from all group modules."""
    handlers: dict[str, Callable[..., Coroutine[Any, Any, list[TextContent]]]] = {}
    for mod in _MODULES:
        handlers.update(mod.HANDLERS)
    return handlers
