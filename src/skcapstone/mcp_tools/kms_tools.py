"""KMS (Key Management Service) tools."""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _home, _json_response

TOOLS: list[Tool] = [
    Tool(
        name="kms_status",
        description=(
            "Get KMS (Key Management Service) status: master key state, "
            "total keys, active/revoked counts, service key inventory."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="kms_list_keys",
        description=(
            "List all keys in the KMS. Shows key ID, type, status, "
            "label, creation date, and rotation count."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "key_type": {
                    "type": "string",
                    "description": "Filter by type: master, service, team, sub (omit for all)",
                },
                "include_revoked": {
                    "type": "boolean",
                    "description": "Include revoked keys (default: false)",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="kms_rotate",
        description=(
            "Rotate a KMS key. Generates a new version of the key "
            "and marks the old version as rotated. The old key material "
            "remains available for decryption."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "key_id": {
                    "type": "string",
                    "description": "The key ID to rotate",
                },
                "reason": {
                    "type": "string",
                    "description": "Reason for rotation (default: 'scheduled')",
                },
            },
            "required": ["key_id"],
        },
    ),
]


async def _handle_kms_status(_args: dict) -> list[TextContent]:
    """Get KMS status."""
    from ..kms import KeyStore

    home = _home()
    store = KeyStore(home)
    store.initialize()
    return _json_response(store.status())


async def _handle_kms_list_keys(args: dict) -> list[TextContent]:
    """List all KMS keys."""
    from ..kms import KeyStore

    home = _home()
    store = KeyStore(home)
    store.initialize()

    key_type = args.get("key_type")
    include_inactive = args.get("include_revoked", False)
    keys = store.list_keys(key_type=key_type, include_inactive=include_inactive)
    return _json_response([
        {
            "key_id": k.key_id,
            "key_type": k.key_type,
            "status": k.status,
            "label": k.label,
            "created_at": str(k.created_at),
            "version": k.version,
            "algorithm": k.algorithm,
        }
        for k in keys
    ])


async def _handle_kms_rotate(args: dict) -> list[TextContent]:
    """Rotate a KMS key."""
    from ..kms import KeyStore

    home = _home()
    store = KeyStore(home)
    store.initialize()

    new_key = store.rotate_key(
        key_id=args["key_id"],
        reason=args.get("reason", "scheduled"),
    )
    return _json_response({
        "key_id": new_key.key_id,
        "version": new_key.version,
        "status": new_key.status,
        "message": f"Key rotated to version {new_key.version}",
    })


HANDLERS: dict = {
    "kms_status": _handle_kms_status,
    "kms_list_keys": _handle_kms_list_keys,
    "kms_rotate": _handle_kms_rotate,
}
