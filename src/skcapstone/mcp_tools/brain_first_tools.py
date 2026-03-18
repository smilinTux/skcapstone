"""Brain-First Protocol MCP tools.

Exposes the brain-first memory consultation as an MCP tool so that
any MCP client can ask "what do I already know about this?" before
acting on a task.
"""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _json_response

TOOLS: list[Tool] = [
    Tool(
        name="brain_first_check",
        description=(
            "Brain-First Protocol: consult the agent's memory before "
            "acting on a task. Extracts keywords from the given context, "
            "searches memory for relevant prior knowledge, and returns "
            "any matching memories. Use this before starting new work to "
            "avoid duplicating effort or missing prior decisions."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "context": {
                    "type": "string",
                    "description": (
                        "The task description, prompt, or action context "
                        "to search memory for"
                    ),
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tag filter for the memory search",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max memories to return (default: from config, usually 5)",
                },
            },
            "required": ["context"],
        },
    ),
]


async def _handle_brain_first_check(args: dict) -> list[TextContent]:
    """Run a brain-first memory consultation."""
    from ..brain_first import BrainFirstConfig, brain_first_check, _load_config

    context = args.get("context", "")
    if not context:
        return _json_response({"error": "context is required"})

    config = _load_config()

    # Allow per-call override of max_results
    max_results = args.get("max_results")
    if max_results is not None:
        config.max_results = max_results

    result = brain_first_check(
        context=context,
        config=config,
        tags=args.get("tags"),
    )

    response = {
        "enabled": result.enabled,
        "query": result.query,
        "keywords": result.keywords,
        "memories_found": len(result.memories),
        "memories": result.memories,
    }

    if result.error:
        response["warning"] = result.error

    if result.has_memories:
        response["context_block"] = result.as_context()

    return _json_response(response)


HANDLERS: dict = {
    "brain_first_check": _handle_brain_first_check,
}
