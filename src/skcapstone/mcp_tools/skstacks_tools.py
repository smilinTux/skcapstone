"""SKStacks v2 secret management tools.

Requires env var SKSTACKS_V2_PATH pointing to the skstacks/v2/ directory
(the directory that contains the ``secrets/`` package).

key format: ``scope/key``  (e.g. "skfence/cloudflare_dns_token").
If no ``/`` is present the entire string is used as the key and the
scope defaults to ``"default"``.
"""

from __future__ import annotations

import os
import sys
from mcp.types import TextContent, Tool

from ._helpers import _error_response, _json_response

TOOLS: list[Tool] = [
    Tool(
        name="capauth_secret_get",
        description=(
            "Retrieve a deployment secret from the SKStacks v2 CapAuth backend "
            "for use in Claude Code and other MCP clients. "
            "Simpler than skstacks_secret_get: no env required — uses "
            "SKSTACKS_ENV (default: prod). "
            "key is the plain secret name; scope groups related keys "
            "(default: 'default'). "
            "Requires SKSTACKS_V2_PATH env var."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Secret key name, e.g. 'cloudflare_dns_token'.",
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Secret scope / service group, e.g. 'skfence'. "
                        "Defaults to 'default'."
                    ),
                },
            },
            "required": ["key"],
        },
    ),
    Tool(
        name="skstacks_secret_get",
        description=(
            "Read a deployment secret from an SKStacks v2 backend. "
            "key may be 'scope/key' (e.g. 'skfence/cloudflare_dns_token') "
            "or plain 'key' (scope defaults to 'default'). "
            "Returns the plaintext value plus metadata. "
            "Requires SKSTACKS_V2_PATH env var."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": (
                        "Secret identifier. Format: 'scope/key' or plain 'key'. "
                        "Example: 'skfence/cloudflare_dns_token'"
                    ),
                },
                "env": {
                    "type": "string",
                    "description": "Target environment: prod, staging, dev, etc.",
                },
                "backend": {
                    "type": "string",
                    "enum": ["vault-file", "hashicorp-vault", "capauth"],
                    "description": (
                        "Secret backend to use. "
                        "Omit to use SKSTACKS_SECRET_BACKEND env var (default: vault-file)."
                    ),
                },
            },
            "required": ["key", "env"],
        },
    ),
    Tool(
        name="skstacks_secret_set",
        description=(
            "Write or update a deployment secret in an SKStacks v2 backend. "
            "key may be 'scope/key' or plain 'key' (scope defaults to 'default'). "
            "The backend versions the old value before overwriting. "
            "Requires SKSTACKS_V2_PATH env var."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": (
                        "Secret identifier. Format: 'scope/key' or plain 'key'. "
                        "Example: 'skfence/cloudflare_dns_token'"
                    ),
                },
                "value": {
                    "type": "string",
                    "description": "Plaintext secret value to store.",
                },
                "env": {
                    "type": "string",
                    "description": "Target environment: prod, staging, dev, etc.",
                },
                "backend": {
                    "type": "string",
                    "enum": ["vault-file", "hashicorp-vault", "capauth"],
                    "description": (
                        "Secret backend to use. "
                        "Omit to use SKSTACKS_SECRET_BACKEND env var (default: vault-file)."
                    ),
                },
            },
            "required": ["key", "value", "env"],
        },
    ),
]


# ── helpers ───────────────────────────────────────────────────────────────────


def _parse_key(raw_key: str) -> tuple[str, str]:
    """Split 'scope/key' → (scope, key).  Plain 'key' → ('default', key)."""
    if "/" in raw_key:
        scope, _, key = raw_key.partition("/")
        return scope.strip(), key.strip()
    return "default", raw_key.strip()


def _load_factory():
    """
    Import ``secrets.factory`` from the SKStacks v2 tree.

    Inserts SKSTACKS_V2_PATH into sys.path so the ``secrets`` package is
    importable.  Raises RuntimeError if the env var is missing or the path
    does not exist.
    """
    v2_path = os.environ.get("SKSTACKS_V2_PATH", "").strip()
    if not v2_path:
        raise RuntimeError(
            "SKSTACKS_V2_PATH is not set. "
            "Point it to the skstacks/v2/ directory."
        )

    import importlib
    from pathlib import Path

    resolved = Path(v2_path).expanduser().resolve()
    if not resolved.is_dir():
        raise RuntimeError(
            f"SKSTACKS_V2_PATH={v2_path!r} does not exist or is not a directory."
        )

    path_str = str(resolved)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

    # Force re-import if module was previously loaded from a different path.
    return importlib.import_module("secrets.factory")


# ── handlers ──────────────────────────────────────────────────────────────────


async def _handle_capauth_secret_get(args: dict) -> list[TextContent]:
    """Retrieve a secret from the capauth backend (simple key/scope API)."""
    key = args.get("key", "").strip()
    scope = (args.get("scope") or "default").strip()

    if not key:
        return _error_response("key is required")

    env = os.environ.get("SKSTACKS_ENV", "prod").strip()

    try:
        factory = _load_factory()
        backend = factory.get_backend("capauth")
    except (RuntimeError, ImportError, ValueError) as exc:
        return _error_response(f"capauth backend init failed: {exc}")

    try:
        value, meta = backend.get_with_meta(scope, env, key)
        return _json_response({
            "ok": True,
            "key": key,
            "scope": scope,
            "env": env,
            "value": value,
            "rotated_at": meta.rotated_at,
        })
    except Exception as exc:
        return _error_response(f"capauth get failed: {type(exc).__name__}: {exc}")
    finally:
        backend.close()


async def _handle_skstacks_secret_get(args: dict) -> list[TextContent]:
    """Retrieve a secret from an SKStacks v2 backend."""
    raw_key = args.get("key", "").strip()
    env = args.get("env", "").strip()
    backend_name = args.get("backend") or None  # None → factory uses env var

    if not raw_key:
        return _error_response("key is required")
    if not env:
        return _error_response("env is required")

    scope, key = _parse_key(raw_key)

    try:
        factory = _load_factory()
        backend = factory.get_backend(backend_name)
    except (RuntimeError, ImportError, ValueError) as exc:
        return _error_response(f"backend init failed: {exc}")

    try:
        value, meta = backend.get_with_meta(scope, env, key)
        return _json_response({
            "ok": True,
            "key": raw_key,
            "scope": scope,
            "env": env,
            "backend": backend_name or os.environ.get("SKSTACKS_SECRET_BACKEND", "vault-file"),
            "value": value,
            "meta": {
                "version": meta.version,
                "created_at": meta.created_at,
                "expires_at": meta.expires_at,
                "rotated_at": meta.rotated_at,
                "tags": meta.tags,
            },
        })
    except Exception as exc:
        # Re-raise as error response — includes SecretNotFoundError, auth errors, etc.
        return _error_response(f"secret get failed: {type(exc).__name__}: {exc}")
    finally:
        backend.close()


async def _handle_skstacks_secret_set(args: dict) -> list[TextContent]:
    """Write or update a secret in an SKStacks v2 backend."""
    raw_key = args.get("key", "").strip()
    value = args.get("value")
    env = args.get("env", "").strip()
    backend_name = args.get("backend") or None

    if not raw_key:
        return _error_response("key is required")
    if value is None:
        return _error_response("value is required")
    if not env:
        return _error_response("env is required")

    scope, key = _parse_key(raw_key)

    try:
        factory = _load_factory()
        backend = factory.get_backend(backend_name)
    except (RuntimeError, ImportError, ValueError) as exc:
        return _error_response(f"backend init failed: {exc}")

    try:
        backend.set(scope, env, key, str(value))
        return _json_response({
            "ok": True,
            "key": raw_key,
            "scope": scope,
            "env": env,
            "backend": backend_name or os.environ.get("SKSTACKS_SECRET_BACKEND", "vault-file"),
            "message": f"Secret '{raw_key}' written to {env} via {backend_name or 'default'} backend.",
        })
    except Exception as exc:
        return _error_response(f"secret set failed: {type(exc).__name__}: {exc}")
    finally:
        backend.close()


HANDLERS: dict = {
    "capauth_secret_get": _handle_capauth_secret_get,
    "skstacks_secret_get": _handle_skstacks_secret_get,
    "skstacks_secret_set": _handle_skstacks_secret_set,
}
