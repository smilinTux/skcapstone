"""CapAuth identity verification and status tools.

Exposes two tools:
    capauth_status — Show CapAuth profile and key status
    capauth_verify — Verify a CapAuth identity or capability token
"""

from __future__ import annotations

import json as _json
import logging

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _home, _json_response

logger = logging.getLogger(__name__)

TOOLS: list[Tool] = [
    Tool(
        name="capauth_status",
        description=(
            "Show CapAuth profile status: whether capauth is installed, "
            "profile loaded, PGP key fingerprint, DID key, and "
            "capability token summary."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="capauth_verify",
        description=(
            "Verify a CapAuth identity or capability token. "
            "Provide either a peer name to verify their identity, "
            "or a capability token string to validate its signature "
            "and expiry."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "peer": {
                    "type": "string",
                    "description": "Peer agent name to verify identity for",
                },
                "token": {
                    "type": "string",
                    "description": "Capability token string to validate",
                },
            },
            "required": [],
        },
    ),
]


async def _handle_capauth_status(_args: dict) -> list[TextContent]:
    """Show CapAuth profile and key status."""
    result: dict = {}

    try:
        import capauth  # type: ignore[import]

        result["installed"] = True
        result["version"] = getattr(capauth, "__version__", "unknown")
    except ImportError:
        result["installed"] = False
        result["version"] = None
        return _json_response(result)

    # Try loading profile
    try:
        from capauth.profile import load_profile  # type: ignore[import]

        profile = load_profile()
        result["profile_loaded"] = True
        result["name"] = getattr(profile, "name", None)
        result["fingerprint"] = getattr(profile, "fingerprint", None)
        result["did_key"] = getattr(profile, "did_key", None)
    except Exception as exc:
        result["profile_loaded"] = False
        result["profile_error"] = str(exc)

    # Check DID key file
    home = _home()
    did_key_file = home / "did" / "did_key.txt"
    if did_key_file.exists():
        try:
            result["did_key_file"] = did_key_file.read_text(encoding="utf-8").strip()
        except Exception as exc:
            logger.warning("Failed to read DID key file: %s", exc)

    # Check identity file
    identity_file = home / "identity" / "identity.json"
    if identity_file.exists():
        try:
            ident = _json.loads(identity_file.read_text(encoding="utf-8"))
            result["identity_name"] = ident.get("name")
            result["identity_fingerprint"] = ident.get("fingerprint")
        except Exception as exc:
            logger.warning("Failed to read identity.json for capauth status: %s", exc)

    return _json_response(result)


async def _handle_capauth_verify(args: dict) -> list[TextContent]:
    """Verify a CapAuth identity or capability token."""
    peer = args.get("peer", "").strip()
    token = args.get("token", "").strip()

    if not peer and not token:
        return _error_response("Provide either 'peer' or 'token' to verify")

    if peer:
        # Verify peer identity via their stored public key
        home = _home()
        peer_file = home / "peers" / f"{peer}.json"

        if not peer_file.exists():
            return _error_response(f"Peer file not found: {peer_file}")

        try:
            peer_data = _json.loads(peer_file.read_text(encoding="utf-8"))
        except Exception as exc:
            return _error_response(f"Could not read peer file: {exc}")

        result = {
            "peer": peer,
            "fingerprint": peer_data.get("fingerprint"),
            "did_key": peer_data.get("did_key"),
            "has_public_key": bool(
                peer_data.get("public_key") or peer_data.get("public_key_armor")
            ),
        }

        # Try capauth DID verification
        try:
            from capauth.did import (  # type: ignore[import]
                _compute_did_key,
                _pgp_armor_to_rsa_numbers,
                _rsa_numbers_to_der,
            )

            pub_armor = peer_data.get("public_key") or peer_data.get("public_key_armor")
            if pub_armor:
                n, e = _pgp_armor_to_rsa_numbers(pub_armor)
                computed = _compute_did_key(_rsa_numbers_to_der(n, e))
                cached = peer_data.get("did_key")
                result["computed_did_key"] = computed
                result["did_match"] = (cached == computed) if cached else None
                result["verified"] = True
            else:
                result["verified"] = False
                result["detail"] = "No public key in peer file"
        except ImportError:
            result["verified"] = False
            result["detail"] = "capauth.did not available for DID verification"
        except Exception as exc:
            result["verified"] = False
            result["detail"] = f"Verification failed: {exc}"

        return _json_response(result)

    if token:
        # Validate a capability token
        try:
            from capauth.tokens import verify_token  # type: ignore[import]

            verification = verify_token(token)
            return _json_response({
                "token_valid": verification.get("valid", False),
                "issuer": verification.get("issuer"),
                "subject": verification.get("subject"),
                "capabilities": verification.get("capabilities", []),
                "expires": verification.get("expires"),
                "detail": verification.get("detail"),
            })
        except ImportError:
            return _error_response(
                "capauth.tokens not available. Install capauth for token verification."
            )
        except Exception as exc:
            return _error_response(f"Token verification failed: {exc}")

    return _error_response("Provide either 'peer' or 'token' to verify")


HANDLERS: dict = {
    "capauth_status": _handle_capauth_status,
    "capauth_verify": _handle_capauth_verify,
}
