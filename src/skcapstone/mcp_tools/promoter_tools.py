"""Memory promoter tools."""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _home, _json_response

TOOLS: list[Tool] = [
    Tool(
        name="promoter_sweep",
        description=(
            "Run a memory promotion sweep. Evaluates memories using "
            "weighted scoring (access frequency, importance, emotion, age, tags) "
            "and promotes qualifying entries to higher tiers."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview promotions without applying (default: false)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max memories to evaluate (default: unlimited)",
                },
                "layer": {
                    "type": "string",
                    "description": "Only evaluate this layer: short-term or mid-term",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="promoter_history",
        description=(
            "View recent memory promotion history \u2014 "
            "shows which memories were promoted, scores, and timestamps."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max entries to return (default: 20)",
                },
            },
            "required": [],
        },
    ),
]


async def _handle_promoter_sweep(args: dict) -> list[TextContent]:
    """Run a memory promotion sweep."""
    from ..memory_promoter import PromotionEngine

    home = _home()
    engine = PromotionEngine(home)

    result = engine.sweep(
        dry_run=args.get("dry_run", False),
        limit=args.get("limit"),
        layer=args.get("layer"),
    )
    return _json_response({
        "scanned": result.scanned,
        "promoted": result.promoted,
        "skipped": result.skipped,
        "dry_run": result.dry_run,
        "by_layer": result.by_layer,
        "promotions": [
            {
                "memory_id": c.memory_id,
                "current_layer": c.current_layer,
                "target_layer": c.target_layer,
                "score": round(c.score, 3),
                "promoted": c.promoted,
            }
            for c in result.candidates
            if c.promoted or result.dry_run
        ],
    })


async def _handle_promoter_history(args: dict) -> list[TextContent]:
    """View promotion history."""
    from ..memory_promoter import PromotionEngine

    home = _home()
    engine = PromotionEngine(home)
    history = engine.get_history(limit=args.get("limit", 20))
    return _json_response(history)


HANDLERS: dict = {
    "promoter_sweep": _handle_promoter_sweep,
    "promoter_history": _handle_promoter_history,
}
