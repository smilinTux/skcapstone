"""Memory Fortress integrity tools."""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _home, _json_response

TOOLS: list[Tool] = [
    Tool(
        name="fortress_verify",
        description=(
            "Verify integrity of all memories in a layer. "
            "Checks HMAC-SHA256 seals to detect tampering."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "layer": {
                    "type": "string",
                    "description": "Memory layer: short-term, mid-term, or long-term (omit for all)",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="fortress_seal_existing",
        description=(
            "Seal all unsealed memories with HMAC-SHA256 integrity seals. "
            "Idempotent \u2014 already-sealed memories are skipped."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="fortress_status",
        description=(
            "Get Memory Fortress status: seal key source, "
            "encryption enabled, total sealed/verified counts."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
]


async def _handle_fortress_verify(args: dict) -> list[TextContent]:
    """Verify memory integrity."""
    from ..memory_fortress import MemoryFortress

    home = _home()
    fortress = MemoryFortress(home)
    fortress.initialize()

    layer = args.get("layer")
    if layer:
        layer_dir = home / "memory" / layer
        if not layer_dir.is_dir():
            return _error_response(f"Layer directory not found: {layer}")
        results = []
        for f in sorted(layer_dir.glob("*.json")):
            _, seal_result = fortress.verify_and_load(f)
            results.append({
                "memory_id": seal_result.memory_id,
                "verified": seal_result.verified,
                "tampered": seal_result.tampered,
                "sealed": seal_result.sealed,
            })
    else:
        seal_results = fortress.verify_all(home)
        results = [
            {
                "memory_id": r.memory_id,
                "verified": r.verified,
                "tampered": r.tampered,
                "sealed": r.sealed,
            }
            for r in seal_results
        ]

    tampered = sum(1 for r in results if r.get("tampered"))
    verified = sum(1 for r in results if r.get("verified"))
    return _json_response({
        "total": len(results),
        "verified": verified,
        "tampered": tampered,
        "unsealed": len(results) - verified - tampered,
        "details": results,
    })


async def _handle_fortress_seal_existing(_args: dict) -> list[TextContent]:
    """Seal all unsealed memories."""
    from ..memory_fortress import MemoryFortress

    home = _home()
    fortress = MemoryFortress(home)
    fortress.initialize()

    sealed_count = fortress.seal_existing(home)
    return _json_response({
        "sealed": sealed_count,
        "message": f"Sealed {sealed_count} previously unsealed memories",
    })


async def _handle_fortress_status(_args: dict) -> list[TextContent]:
    """Get Memory Fortress status."""
    from ..memory_fortress import MemoryFortress

    home = _home()
    fortress = MemoryFortress(home)
    fortress.initialize()
    return _json_response(fortress.status())


HANDLERS: dict = {
    "fortress_verify": _handle_fortress_verify,
    "fortress_seal_existing": _handle_fortress_seal_existing,
    "fortress_status": _handle_fortress_status,
}
