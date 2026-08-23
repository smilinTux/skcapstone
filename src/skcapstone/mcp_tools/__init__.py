"""MCP tool group modules - split from mcp_server.py for maintainability.

Each module exposes:
    TOOLS:    list[Tool]   - MCP tool definitions
    HANDLERS: dict         - {tool_name: async_handler_fn}
    HIDDEN:   set[str]     - (optional) tool names present in the module but
                             intentionally NOT published on the MCP wire surface.

The ``collect_all_tools`` and ``collect_all_handlers`` functions aggregate
across every module so mcp_server.py can register them in one shot. Names listed
in a module's ``HIDDEN`` set are skipped by both aggregators.
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine

from mcp.types import TextContent, Tool

from . import (
    agent_tools,
    ansible_tools,
    brain_first_tools,
    capauth_tools,
    chat_tools,
    cloud9_tools,
    comm_tools,
    consciousness_tools,
    coord_card_tools,
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
    security_tools,
    skchat_tools,
    skcomms_tools,
    skills_tools,
    skseed_tools,
    skstacks_tools,
    soul_tools,
    suggest_tools,
    sync_tools,
    telegram_tools,
    trust_tools,
    trustee_tools,
    version_tools,
)

# Ordered list of all tool-group modules.
_MODULES = [
    agent_tools,
    brain_first_tools,
    memory_tools,
    comm_tools,
    sync_tools,
    coord_tools,
    coord_card_tools,
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
    suggest_tools,
    deploy_tools,
    model_tools,
    consciousness_tools,
    emotion_tools,
    notification_tools,
    telegram_tools,
    capauth_tools,
    cloud9_tools,
    security_tools,
    skchat_tools,
    skcomms_tools,
    version_tools,
]


def collect_all_tools() -> list[Tool]:
    """Return every published Tool definition from all group modules.

    Tools whose name is listed in a module's ``HIDDEN`` set are omitted so the
    MCP wire surface stays byte-identical to the historical inline definition.
    """
    tools: list[Tool] = []
    for mod in _MODULES:
        hidden = getattr(mod, "HIDDEN", set())
        tools.extend(t for t in mod.TOOLS if t.name not in hidden)
    return tools


def collect_all_handlers() -> dict[str, Callable[..., Coroutine[Any, Any, list[TextContent]]]]:
    """Return a merged {name: handler} dict from all group modules.

    Handlers for names listed in a module's ``HIDDEN`` set are omitted so the
    dispatch surface matches the published tool list exactly.
    """
    handlers: dict[str, Callable[..., Coroutine[Any, Any, list[TextContent]]]] = {}
    for mod in _MODULES:
        hidden = getattr(mod, "HIDDEN", set())
        handlers.update({k: v for k, v in mod.HANDLERS.items() if k not in hidden})
    return handlers
