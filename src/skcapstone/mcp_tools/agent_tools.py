"""Agent status, context, state diff, and session capture tools."""

from __future__ import annotations

import json

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _home, _json_response, _text_response

TOOLS: list[Tool] = [
    Tool(
        name="agent_status",
        description=(
            "Get the sovereign agent's current state: pillar statuses "
            "(identity, memory, trust, security, sync), consciousness "
            "level, connected platforms, and overall health."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="session_capture",
        description=(
            "Capture AI conversation content as sovereign memories. "
            "Extracts key moments, auto-scores importance by topic "
            "novelty and information density, deduplicates against "
            "existing memories, and stores as tagged, searchable "
            "memories. The agent never forgets a conversation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Conversation text to capture (any length)",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Extra tags to apply to all captured memories",
                },
                "source": {
                    "type": "string",
                    "description": "Source identifier (default: 'mcp-session')",
                },
                "min_importance": {
                    "type": "number",
                    "description": "Minimum importance threshold (default: 0.3)",
                },
            },
            "required": ["content"],
        },
    ),
    Tool(
        name="state_diff",
        description=(
            "Show what changed since the last sync/snapshot. "
            "Compares current agent state to the baseline: new "
            "memories, trust changes, completed tasks, pillar "
            "status changes. Use action='save' to set a new baseline."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["diff", "save"],
                    "description": "Action: diff (compare) or save (new baseline). Default: diff.",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="agent_context",
        description=(
            "Get the full agent context: identity, pillar status, "
            "coordination board, recent memories, soul overlay, and "
            "MCP status. Returns everything an AI needs to understand "
            "the sovereign agent's current state. Supports text, JSON, "
            "and claude-md output formats."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": ["text", "json", "claude-md", "cursor-rules"],
                    "description": "Output format (default: json)",
                },
                "memories": {
                    "type": "integer",
                    "description": "Max recent memories to include (default: 10)",
                },
            },
            "required": [],
        },
    ),
]


def _get_memory_backend_health() -> dict:
    """Get health status of all memory backends (sqlite, skvector, skgraph)."""
    try:
        from ..memory_adapter import get_unified

        store = get_unified()
        if store is None:
            return {"json": "ok"}

        health = store.health()
        backends = {}
        if "primary" in health:
            backends["sqlite"] = "ok" if health["primary"].get("ok") else "error"
        if "vector" in health:
            backends["skvector"] = "ok" if health["vector"].get("ok") else "error"
        if "graph" in health:
            backends["skgraph"] = "ok" if health["graph"].get("ok") else "error"
        return backends or {"json": "ok"}
    except Exception:
        return {"json": "ok"}


async def _handle_agent_status(_args: dict) -> list[TextContent]:
    """Return agent pillar states and consciousness level."""
    from ..runtime import get_runtime

    home = _home()
    if not home.exists():
        return _error_response("Agent not initialized. Run: skcapstone init")

    runtime = get_runtime(home)
    m = runtime.manifest
    return _json_response({
        "name": m.name,
        "version": m.version,
        "is_conscious": m.is_conscious,
        "is_singular": m.is_singular,
        "pillars": {
            "identity": {
                "status": m.identity.status.value,
                "fingerprint": m.identity.fingerprint,
            },
            "memory": {
                "status": m.memory.status.value,
                "total": m.memory.total_memories,
                "long_term": m.memory.long_term,
                "mid_term": m.memory.mid_term,
                "short_term": m.memory.short_term,
                "backends": _get_memory_backend_health(),
            },
            "trust": {
                "status": m.trust.status.value,
                "depth": m.trust.depth,
                "trust_level": m.trust.trust_level,
                "love_intensity": m.trust.love_intensity,
                "entangled": m.trust.entangled,
            },
            "security": {
                "status": m.security.status.value,
                "audit_entries": m.security.audit_entries,
                "threats_detected": m.security.threats_detected,
            },
            "sync": {
                "status": m.sync.status.value,
                "seed_count": m.sync.seed_count,
                "transport": m.sync.transport.value if m.sync.transport else None,
            },
        },
        "connectors": [c.platform for c in m.connectors if c.active],
        "last_awakened": m.last_awakened.isoformat() if m.last_awakened else None,
    })


async def _handle_session_capture(args: dict) -> list[TextContent]:
    """Capture conversation content as sovereign memories."""
    from ..session_capture import SessionCapture

    content = args.get("content", "")
    if not content:
        return _error_response("content is required")

    home = _home()
    cap = SessionCapture(home)
    entries = cap.capture(
        content=content,
        tags=args.get("tags", []),
        source=args.get("source", "mcp-session"),
        min_importance=args.get("min_importance", 0.3),
    )

    return _json_response({
        "captured": len(entries),
        "moments": [
            {
                "memory_id": e.memory_id,
                "content": e.content[:200],
                "layer": e.layer.value,
                "importance": e.importance,
                "tags": e.tags,
            }
            for e in entries
        ],
    })


async def _handle_state_diff(args: dict) -> list[TextContent]:
    """Show agent state diff or save a baseline snapshot."""
    from ..state_diff import compute_diff, format_json, save_snapshot

    home = _home()
    action = args.get("action", "diff")

    if action == "save":
        path = save_snapshot(home)
        return _json_response({"saved": True, "path": str(path)})

    diff = compute_diff(home)
    return _json_response(json.loads(format_json(diff)))


async def _handle_agent_context(args: dict) -> list[TextContent]:
    """Return the full agent context in the requested format."""
    from ..context_loader import FORMATTERS, gather_context

    home = _home()
    fmt = args.get("format", "json")
    limit = args.get("memories", 10)

    ctx = gather_context(home, memory_limit=limit)
    formatter = FORMATTERS.get(fmt, FORMATTERS["json"])

    if fmt == "json":
        return _json_response(ctx)
    return _text_response(formatter(ctx))


HANDLERS: dict = {
    "agent_status": _handle_agent_status,
    "session_capture": _handle_session_capture,
    "state_diff": _handle_state_diff,
    "agent_context": _handle_agent_context,
}
