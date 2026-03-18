"""Memory store, search, recall, and curation tools."""

from __future__ import annotations

import logging

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _home, _json_response

logger = logging.getLogger(__name__)

TOOLS: list[Tool] = [
    Tool(
        name="memory_store",
        description=(
            "Store a new memory in the agent's persistent memory. "
            "Memories start in short-term and promote based on "
            "access patterns and importance."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The memory content (free-text)",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags for categorization",
                },
                "importance": {
                    "type": "number",
                    "description": "Importance score 0.0-1.0 (>= 0.7 auto-promotes to mid-term)",
                },
                "source": {
                    "type": "string",
                    "description": "Where this memory came from (default: mcp)",
                },
            },
            "required": ["content"],
        },
    ),
    Tool(
        name="memory_search",
        description=(
            "Search the agent's memories by query string. "
            "Full-text search across all layers, ranked by relevance."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default: 10)",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter by tags (all must match)",
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="memory_recall",
        description=(
            "Recall a specific memory by its ID. Returns full content "
            "and increments the access counter (frequent access promotes memories)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "The memory's unique ID",
                },
            },
            "required": ["memory_id"],
        },
    ),
    Tool(
        name="memory_curate",
        description=(
            "Run a curation pass over the agent's memories. "
            "Auto-tags untagged memories, promotes qualifying "
            "memories to higher tiers, and removes duplicates. "
            "Use dry_run=true to preview without changes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview changes without applying (default: false)",
                },
                "stats_only": {
                    "type": "boolean",
                    "description": "Return statistics instead of curating (default: false)",
                },
            },
            "required": [],
        },
    ),
]


async def _handle_memory_store(args: dict) -> list[TextContent]:
    """Store a new memory."""
    from ..memory_engine import store

    content = args.get("content", "")
    if not content:
        return _error_response("content is required")

    entry = store(
        home=_home(),
        content=content,
        tags=args.get("tags", []),
        source=args.get("source", "mcp"),
        importance=args.get("importance", 0.5),
    )
    try:
        from .. import activity
        activity.push("memory.stored", {
            "memory_id": entry.memory_id,
            "layer": entry.layer.value,
            "importance": entry.importance,
            "tags": entry.tags,
        })
    except Exception as exc:
        logger.warning("Failed to push memory.stored activity for %s: %s", entry.memory_id, exc)
    return _json_response({
        "memory_id": entry.memory_id,
        "layer": entry.layer.value,
        "importance": entry.importance,
        "tags": entry.tags,
        "stored": True,
    })


async def _handle_memory_search(args: dict) -> list[TextContent]:
    """Search memories by query."""
    from ..memory_engine import search

    query = args.get("query", "")
    if not query:
        return _error_response("query is required")

    results = search(
        home=_home(),
        query=query,
        tags=args.get("tags"),
        limit=args.get("limit", 10),
    )
    return _json_response([
        {
            "memory_id": e.memory_id,
            "layer": e.layer.value,
            "content": e.content[:300],
            "tags": e.tags,
            "importance": e.importance,
            "access_count": e.access_count,
        }
        for e in results
    ])


async def _handle_memory_recall(args: dict) -> list[TextContent]:
    """Recall a specific memory by ID."""
    from ..memory_engine import recall

    memory_id = args.get("memory_id", "")
    if not memory_id:
        return _error_response("memory_id is required")

    entry = recall(home=_home(), memory_id=memory_id)
    if entry is None:
        return _error_response(f"Memory not found: {memory_id}")

    return _json_response({
        "memory_id": entry.memory_id,
        "content": entry.content,
        "layer": entry.layer.value,
        "tags": entry.tags,
        "importance": entry.importance,
        "access_count": entry.access_count,
        "source": entry.source,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    })


async def _handle_memory_curate(args: dict) -> list[TextContent]:
    """Run a memory curation pass or return stats."""
    from ..memory_curator import MemoryCurator

    home = _home()
    curator = MemoryCurator(home)

    if args.get("stats_only"):
        return _json_response(curator.get_stats())

    dry_run = args.get("dry_run", False)
    result = curator.curate(dry_run=dry_run)
    return _json_response({
        "dry_run": dry_run,
        "scanned": result.total_scanned,
        "tagged": len(result.tagged),
        "promoted": len(result.promoted),
        "deduped": len(result.deduped),
        "by_layer": result.by_layer,
    })


HANDLERS: dict = {
    "memory_store": _handle_memory_store,
    "memory_search": _handle_memory_search,
    "memory_recall": _handle_memory_recall,
    "memory_curate": _handle_memory_curate,
}
