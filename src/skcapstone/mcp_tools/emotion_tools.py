"""Emotion tracker MCP tools — trend analysis and warmth anchor insight."""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _home, _json_response, _error_response

TOOLS: list[Tool] = [
    Tool(
        name="emotion_trend",
        description=(
            "Return the 7-day rolling emotion trend from the consciousness loop. "
            "Shows sentiment distribution (positive/neutral/concerned/excited), "
            "average valence score 0–1, trend direction (improving/stable/declining), "
            "and the recommended warmth anchor value derived from recent emotions. "
            "Optionally query a different lookback window with the 'days' parameter."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days (default 7, max 30)",
                    "default": 7,
                },
            },
            "required": [],
        },
    ),
]


async def _handle_emotion_trend(arguments: dict) -> list[TextContent]:
    """Handle emotion_trend tool call."""
    days = int(arguments.get("days", 7))
    days = max(1, min(days, 30))

    try:
        from ..emotion_tracker import EmotionTracker
        from ..warmth_anchor import get_anchor

        home = _home()
        tracker = EmotionTracker(home=home)
        trend = tracker.get_trend(days=days)

        # Attach current warmth anchor value for comparison
        try:
            anchor = get_anchor(home)
            trend["current_warmth"] = anchor.get("warmth", None)
        except Exception:
            trend["current_warmth"] = None

        return _json_response(trend)

    except Exception as exc:
        return _error_response(f"emotion_trend failed: {exc}")


HANDLERS = {
    "emotion_trend": _handle_emotion_trend,
}
