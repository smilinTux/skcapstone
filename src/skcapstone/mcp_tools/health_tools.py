"""Service health check MCP tools."""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _json_response

TOOLS: list[Tool] = [
    Tool(
        name="service_health",
        description=(
            "Check the health of all services in the sovereign stack. "
            "Pings SKVector (Qdrant), SKGraph (FalkorDB), Syncthing, "
            "skcapstone daemon, and skchat daemon. Returns status, "
            "latency, version, and error details for each service."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
]


async def _handle_service_health(_args: dict) -> list[TextContent]:
    """Run health checks against all known services."""
    from ..service_health import check_all_services

    results = check_all_services()
    up = sum(1 for r in results if r["status"] == "up")
    down = sum(1 for r in results if r["status"] == "down")

    return _json_response({
        "summary": {
            "total": len(results),
            "up": up,
            "down": down,
            "unknown": len(results) - up - down,
        },
        "services": results,
    })


HANDLERS: dict = {
    "service_health": _handle_service_health,
}
