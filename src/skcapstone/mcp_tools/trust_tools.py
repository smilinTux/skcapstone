"""Trust calibration and trust graph tools."""

from __future__ import annotations

import json

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _home, _json_response, _text_response

TOOLS: list[Tool] = [
    Tool(
        name="trust_calibrate",
        description=(
            "View, recommend, or update trust layer calibration "
            "thresholds. Controls how FEB data maps to trust state: "
            "entanglement depth, conscious trust level, love thresholds, "
            "and aggregation strategy."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["show", "recommend", "set", "reset"],
                    "description": "Action: show current, recommend changes, set a value, or reset (default: show)",
                },
                "key": {
                    "type": "string",
                    "description": "Threshold key to set (for action=set)",
                },
                "value": {
                    "type": "string",
                    "description": "New value (for action=set)",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="trust_graph",
        description=(
            "Visualize the trust web: PGP key signatures, capability "
            "token chains, FEB entanglement, sync peers, and coordination "
            "collaborators. Returns a graph of who trusts whom."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": ["json", "dot", "table"],
                    "description": "Output format (default: json)",
                },
            },
            "required": [],
        },
    ),
]


async def _handle_trust_calibrate(args: dict) -> list[TextContent]:
    """View, recommend, or update trust calibration."""
    from ..trust_calibration import (
        TrustThresholds,
        apply_setting,
        load_calibration,
        recommend_thresholds,
        save_calibration,
    )

    home = _home()
    action = args.get("action", "show")

    if action == "show":
        cal = load_calibration(home)
        return _json_response(cal.model_dump())

    if action == "recommend":
        return _json_response(recommend_thresholds(home))

    if action == "set":
        key = args.get("key", "")
        value = args.get("value", "")
        if not key or not value:
            return _error_response("key and value are required for action=set")
        try:
            updated = apply_setting(home, key, value)
            return _json_response({"updated": True, "key": key, "value": value, "thresholds": updated.model_dump()})
        except ValueError as exc:
            return _error_response(str(exc))

    if action == "reset":
        save_calibration(home, TrustThresholds())
        return _json_response({"reset": True, "thresholds": TrustThresholds().model_dump()})

    return _error_response(f"Unknown action: {action}")


async def _handle_trust_graph(args: dict) -> list[TextContent]:
    """Return the trust web graph."""
    from ..trust_graph import FORMATTERS as TG_FORMATTERS
    from ..trust_graph import build_trust_graph

    home = _home()
    graph = build_trust_graph(home)
    fmt = args.get("format", "json")
    formatter = TG_FORMATTERS.get(fmt, TG_FORMATTERS["json"])

    if fmt == "json":
        return _json_response(json.loads(formatter(graph)))
    return _text_response(formatter(graph))


HANDLERS: dict = {
    "trust_calibrate": _handle_trust_calibrate,
    "trust_graph": _handle_trust_graph,
}
