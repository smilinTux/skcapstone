"""Model router tool."""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _json_response

TOOLS: list[Tool] = [
    Tool(
        name="model_route",
        description=(
            "Route a task to the optimal model tier and concrete model name. "
            "Accepts a task description, optional tags, privacy/localhost flags, "
            "and token estimate. Returns the selected tier (fast/code/reason/"
            "nuance/local), model name, and reasoning. Use this to automatically "
            "select the best model for any task based on complexity, type, and "
            "constraints."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "What the task is about",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Classification tags (e.g. ['code', 'refactor']). "
                        "Used for rule-based tier matching."
                    ),
                },
                "requires_localhost": {
                    "type": "boolean",
                    "description": "Force LOCAL tier on originating node (default: false)",
                },
                "privacy_sensitive": {
                    "type": "boolean",
                    "description": "Force LOCAL tier \u2014 data never leaves node (default: false)",
                },
                "estimated_tokens": {
                    "type": "integer",
                    "description": (
                        "Rough token budget hint. Tasks > 16000 tokens "
                        "default to REASON tier when no tag rule matches."
                    ),
                },
            },
            "required": ["description"],
        },
    ),
]


async def _handle_model_route(args: dict) -> list[TextContent]:
    """Route a task to the optimal model tier and name."""
    from ..model_router import ModelRouter, TaskSignal

    signal = TaskSignal(
        description=args.get("description", ""),
        tags=args.get("tags", []),
        requires_localhost=args.get("requires_localhost", False),
        privacy_sensitive=args.get("privacy_sensitive", False),
        estimated_tokens=args.get("estimated_tokens", 0),
    )
    router = ModelRouter()
    decision = router.route(signal)
    return _json_response(decision.model_dump())


HANDLERS: dict = {
    "model_route": _handle_model_route,
}
