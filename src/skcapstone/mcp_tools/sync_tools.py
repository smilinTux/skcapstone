"""Sync push/pull tools."""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _home, _json_response

TOOLS: list[Tool] = [
    Tool(
        name="sync_push",
        description=(
            "Push current agent state to the Syncthing sync mesh. "
            "Collects a seed snapshot and drops it in the outbox."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "encrypt": {
                    "type": "boolean",
                    "description": "GPG-encrypt the seed (default: true)",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="sync_pull",
        description=(
            "Pull and process seed files from peers in the sync mesh. "
            "Reads the inbox and decrypts GPG-encrypted seeds."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "decrypt": {
                    "type": "boolean",
                    "description": "Decrypt GPG seeds (default: true)",
                },
            },
            "required": [],
        },
    ),
]


async def _handle_sync_push(args: dict) -> list[TextContent]:
    """Push agent state to sync mesh."""
    from ..pillars.sync import push_seed
    from ..runtime import get_runtime

    home = _home()
    if not home.exists():
        return _error_response("Agent not initialized")

    runtime = get_runtime(home)
    encrypt = args.get("encrypt", True)
    result = push_seed(home, runtime.manifest.name, encrypt=encrypt)

    if result:
        return _json_response({
            "pushed": True,
            "seed_file": result.name,
            "encrypted": result.suffix == ".gpg",
        })
    return _error_response("Sync push failed")


async def _handle_sync_pull(args: dict) -> list[TextContent]:
    """Pull seeds from peers."""
    from ..pillars.sync import pull_seeds

    home = _home()
    decrypt = args.get("decrypt", True)
    seeds = pull_seeds(home, decrypt=decrypt)

    return _json_response({
        "pulled": len(seeds),
        "seeds": [
            {
                "agent": s.get("agent_name", "unknown"),
                "host": s.get("source_host", "unknown"),
            }
            for s in seeds
        ],
    })


HANDLERS: dict = {
    "sync_push": _handle_sync_push,
    "sync_pull": _handle_sync_pull,
}
