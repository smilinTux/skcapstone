"""Consciousness loop MCP tools — status and testing."""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _home, _json_response, _text_response, _error_response

TOOLS: list[Tool] = [
    Tool(
        name="consciousness_status",
        description=(
            "Get consciousness loop status: enabled state, messages processed, "
            "responses sent, errors, backend health, inotify state, and "
            "active conversations."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="consciousness_test",
        description=(
            "Test the consciousness pipeline end-to-end with a message. "
            "Classifies the message, builds the agent system prompt, routes "
            "to the appropriate LLM, and returns the response."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The test message to process",
                },
            },
            "required": ["message"],
        },
    ),
    Tool(
        name="context_stats",
        description=(
            "Show per-sender context-window token usage for the consciousness "
            "loop: token count, message count, percent of the model context "
            "budget used, the compression threshold (80%), and when each "
            "sender's history was last compressed. Optionally filter to one peer."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "peer": {
                    "type": "string",
                    "description": "Optional peer name to filter to a single sender.",
                },
            },
            "required": [],
        },
    ),
]


async def _handle_consciousness_status(arguments: dict) -> list[TextContent]:
    """Handle consciousness_status tool call."""
    try:
        import urllib.request
        import json

        url = "http://127.0.0.1:7777/consciousness"
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read())
            return _json_response(data)
    except Exception:
        # Fallback: try to load directly
        try:
            from ..consciousness_config import load_consciousness_config
            from ..consciousness_loop import ConsciousnessConfig, LLMBridge

            home = _home()
            config = load_consciousness_config(home)
            bridge = LLMBridge(config)

            return _json_response({
                "enabled": config.enabled,
                "backends": bridge.health_check(),
                "daemon_reachable": False,
                "note": "Loaded directly (daemon not running)",
            })
        except Exception as exc:
            return _error_response(f"Cannot get consciousness status: {exc}")


async def _handle_consciousness_test(arguments: dict) -> list[TextContent]:
    """Handle consciousness_test tool call."""
    message = arguments.get("message", "")
    if not message:
        return _error_response("message is required")

    try:
        from ..consciousness_config import load_consciousness_config
        from ..consciousness_loop import (
            LLMBridge,
            SystemPromptBuilder,
            _classify_message,
        )

        home = _home()
        config = load_consciousness_config(home)
        bridge = LLMBridge(config)
        builder = SystemPromptBuilder(home, config.max_context_tokens)

        signal = _classify_message(message)
        system_prompt = builder.build()
        response = bridge.generate(system_prompt, message, signal)

        return _json_response({
            "message": message,
            "signal": {
                "tags": signal.tags,
                "estimated_tokens": signal.estimated_tokens,
            },
            "system_prompt_length": len(system_prompt),
            "response": response,
            "response_length": len(response),
        })
    except Exception as exc:
        return _error_response(f"Consciousness test failed: {exc}")


async def _handle_context_stats(arguments: dict) -> list[TextContent]:
    """Handle context_stats tool call — per-sender context-window usage."""
    try:
        from ..consciousness_config import load_consciousness_config
        from ..context_window import ContextWindowManager
        from ..conversation_store import ConversationStore

        home = _home()
        config = load_consciousness_config(home)
        store = ConversationStore(home)
        mgr = ContextWindowManager(home, config.max_context_tokens)
        stats = mgr.get_all_stats(store)

        peer = arguments.get("peer")
        if peer:
            stats = {peer: stats.get(peer, {})}

        return _json_response({
            "max_context_tokens": config.max_context_tokens,
            "threshold_pct": 80,
            "senders": stats,
            "sender_count": len(stats),
        })
    except Exception as exc:
        return _error_response(f"Cannot get context stats: {exc}")


HANDLERS = {
    "consciousness_status": _handle_consciousness_status,
    "consciousness_test": _handle_consciousness_test,
    "context_stats": _handle_context_stats,
}
