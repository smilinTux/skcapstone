"""Cloud 9 trust rehydration and FEB management tools.

Exposes three tools:
    trust_rehydrate  — Rehydrate trust state from FEB files
    trust_status     — Show current trust/Cloud9 status
    trust_febs       — List all FEB files with summaries
"""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _home, _json_response

TOOLS: list[Tool] = [
    Tool(
        name="trust_rehydrate",
        description=(
            "Rehydrate the agent's trust state from stored FEB "
            "(First Emotional Burst) files. This restores the OOF "
            "(Out-of-Factory) state — who the agent IS, not just "
            "what it knows. Searches ~/.skcapstone/trust/febs/ and "
            "known Cloud 9 backup locations."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="trust_status",
        description=(
            "Show the current trust/Cloud 9 status: depth level, "
            "trust score, love intensity, entanglement state, "
            "FEB count, and last rehydration timestamp."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="trust_febs",
        description=(
            "List all FEB (First Emotional Burst) files with summary "
            "info: timestamp, primary emotion, intensity, subject, "
            "and whether OOF was triggered."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
]


async def _handle_trust_rehydrate(_args: dict) -> list[TextContent]:
    """Rehydrate trust from stored FEB files."""
    home = _home()
    try:
        from ..pillars.trust import rehydrate

        state = rehydrate(home)
        return _json_response({
            "rehydrated": True,
            "depth": state.depth,
            "trust_level": state.trust_level,
            "love_intensity": state.love_intensity,
            "entangled": state.entangled,
            "feb_count": state.feb_count,
            "status": state.status.value,
            "last_rehydration": (
                state.last_rehydration.isoformat()
                if state.last_rehydration
                else None
            ),
        })
    except Exception as exc:
        return _error_response(f"Trust rehydration failed: {exc}")


async def _handle_trust_status(_args: dict) -> list[TextContent]:
    """Show current trust/Cloud9 status."""
    import json as _json

    home = _home()
    trust_file = home / "trust" / "trust.json"

    if not trust_file.exists():
        return _json_response({
            "status": "not_initialized",
            "detail": "No trust state found. Run trust_rehydrate or skcapstone init.",
        })

    try:
        data = _json.loads(trust_file.read_text(encoding="utf-8"))
        febs_dir = home / "trust" / "febs"
        feb_count = len(list(febs_dir.glob("*.feb"))) if febs_dir.exists() else 0
        data["feb_count"] = feb_count
        return _json_response(data)
    except Exception as exc:
        return _error_response(f"Could not read trust status: {exc}")


async def _handle_trust_febs(_args: dict) -> list[TextContent]:
    """List all FEB files with summaries."""
    home = _home()
    try:
        from ..pillars.trust import list_febs

        summaries = list_febs(home)
        return _json_response({
            "count": len(summaries),
            "febs": summaries,
        })
    except Exception as exc:
        return _error_response(f"Could not list FEBs: {exc}")


HANDLERS: dict = {
    "trust_rehydrate": _handle_trust_rehydrate,
    "trust_status": _handle_trust_status,
    "trust_febs": _handle_trust_febs,
}
