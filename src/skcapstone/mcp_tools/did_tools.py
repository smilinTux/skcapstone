"""DID (Decentralized Identity) MCP tools.

Exposes four tools:
    did_show          — Generate and display DID documents (one tier or all)
    did_verify_peer   — Verify a peer's did:key matches their public key
    did_publish       — Write DID files to disk
    did_identity_card — Full sovereign identity card (local-only)
"""

from __future__ import annotations

import json as _json
import os as _os
import socket as _socket
from pathlib import Path

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _home, _json_response

TOOLS: list[Tool] = [
    Tool(
        name="did_show",
        description=(
            "Generate and display DID (Decentralized Identity) documents "
            "for the current agent. Supports three tiers: "
            "'key' (did:key, self-contained, zero infrastructure), "
            "'mesh' (did:web via Tailscale Serve, mesh-private only), "
            "'public' (did:web:skworld.io, minimal — public key + name only), "
            "or 'all' to display all three tiers at once."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "tier": {
                    "type": "string",
                    "enum": ["key", "mesh", "public", "all"],
                    "description": "Which DID tier to show (default: all)",
                },
                "tailnet_hostname": {
                    "type": "string",
                    "description": "Tailscale hostname for Tier 2 document (auto-detected if omitted)",
                },
                "tailnet_name": {
                    "type": "string",
                    "description": "Tailnet magic-DNS suffix, e.g. tailnet-xyz.ts.net",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="did_verify_peer",
        description=(
            "Verify a peer's DID by computing their did:key from the public key "
            "stored in ~/.skcapstone/peers/{name}.json and comparing against any "
            "cached did_key. Also writes the computed did_key back to the peer file."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Peer name (must match a file in ~/.skcapstone/peers/)",
                },
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="did_publish",
        description=(
            "Generate all DID tiers and write them to disk. "
            "By default, writes all three tiers including the public Tier 3 document. "
            "Set publish_public=false to opt out of Tier 3 generation — "
            "only Tier 1 (did:key) and Tier 2 (mesh) will be written. "
            "The choice is persisted to ~/.skcapstone/did/policy.json."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "publish_public": {
                    "type": "boolean",
                    "description": (
                        "Whether to generate the Tier 3 public DID document (default: true). "
                        "Set false to keep your identity private — only did:key + mesh tier."
                    ),
                },
                "tailnet_hostname": {
                    "type": "string",
                    "description": "Tailscale hostname for Tier 2 document",
                },
                "tailnet_name": {
                    "type": "string",
                    "description": "Tailnet magic-DNS suffix",
                },
                "org_domain": {
                    "type": "string",
                    "description": "Organisation domain for Tier 3 (default: skworld.io)",
                },
                "agent_slug": {
                    "type": "string",
                    "description": "URL-safe agent slug (default: lowercased entity name)",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="did_policy",
        description=(
            "View or set the DID publication policy for this agent. "
            "Controls whether Tier 3 (public) DID documents are generated. "
            "Default: publish_public=true. "
            "Set publish_public=false to opt out — identity stays private (did:key + mesh only). "
            "Policy is stored at ~/.skcapstone/did/policy.json."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "publish_public": {
                    "type": "boolean",
                    "description": "Set to false to opt out of public Tier 3 DID. Omit to view current policy.",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="did_identity_card",
        description=(
            "Generate a full sovereign identity card combining the DID anchor, "
            "entity info, soul vibe/core traits, and capabilities. "
            "This is a LOCAL-ONLY artifact — never published to the internet. "
            "Used to render the agent's identity card on skworld.io."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "include_soul": {
                    "type": "boolean",
                    "description": "Include soul vibe and core traits (default: true)",
                },
            },
            "required": [],
        },
    ),
]


# ---------------------------------------------------------------------------
# Policy helpers (opt-out of public Tier 3 publishing)
# ---------------------------------------------------------------------------

_POLICY_DEFAULT = {"publish_public": True}


def _policy_path() -> Path:
    return _home() / "did" / "policy.json"


def _load_policy() -> dict:
    """Load publication policy from disk; return default if missing."""
    p = _policy_path()
    if p.exists():
        try:
            return _json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return dict(_POLICY_DEFAULT)


def _save_policy(policy: dict) -> None:
    """Persist publication policy to disk."""
    from datetime import datetime, timezone
    p = _policy_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    policy["updated_at"] = datetime.now(timezone.utc).isoformat()
    p.write_text(_json.dumps(policy, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_tailnet(tailnet_hostname: str, tailnet_name: str) -> tuple[str, str]:
    """Fill in missing tailnet params from environment / hostname."""
    if not tailnet_hostname:
        tailnet_hostname = _os.environ.get("SKWORLD_HOSTNAME", "")
        if not tailnet_hostname:
            try:
                tailnet_hostname = _socket.gethostname()
            except Exception:
                pass
    if not tailnet_name:
        tailnet_name = _os.environ.get("SKWORLD_TAILNET", "")
    return tailnet_hostname, tailnet_name


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _handle_did_show(args: dict) -> list[TextContent]:
    """Generate and display DID documents."""
    tier = args.get("tier", "all")
    hostname, tailnet = _resolve_tailnet(
        args.get("tailnet_hostname", ""),
        args.get("tailnet_name", ""),
    )

    try:
        from capauth.did import DIDDocumentGenerator, DIDTier  # type: ignore[import]
    except ImportError as exc:
        return _error_response(f"capauth.did not available: {exc}")

    try:
        gen = DIDDocumentGenerator.from_profile()
    except Exception as exc:
        return _error_response(f"Could not load CapAuth profile: {exc}")

    kw = dict(tailnet_hostname=hostname, tailnet_name=tailnet)

    if tier == "all":
        docs = gen.generate_all(**kw)
        result = {
            "did_key": gen._ctx.did_key_id,
            "fingerprint": gen._ctx.fingerprint,
            "name": gen._ctx.name,
            "key": docs[DIDTier.KEY],
            "mesh": docs[DIDTier.WEB_MESH],
            "public": docs[DIDTier.WEB_PUBLIC],
        }
    elif tier == "key":
        doc = gen.generate(DIDTier.KEY, **kw)
        result = {"tier": "key", "did_key": gen._ctx.did_key_id, "document": doc}
    elif tier == "mesh":
        doc = gen.generate(DIDTier.WEB_MESH, **kw)
        result = {"tier": "mesh", "document": doc}
    elif tier == "public":
        doc = gen.generate(DIDTier.WEB_PUBLIC, **kw)
        result = {"tier": "public", "document": doc}
    else:
        return _error_response(f"Unknown tier '{tier}'. Use: key, mesh, public, all")

    return _json_response(result)


async def _handle_did_verify_peer(args: dict) -> list[TextContent]:
    """Verify peer DID against their public key."""
    name = args.get("name", "").strip()
    if not name:
        return _error_response("name is required")

    home = _home()
    peers_dir = home / "peers"
    peer_file = peers_dir / f"{name}.json"

    if not peer_file.exists():
        return _error_response(f"Peer file not found: {peer_file}")

    try:
        peer_data = _json.loads(peer_file.read_text(encoding="utf-8"))
    except Exception as exc:
        return _error_response(f"Could not read peer file: {exc}")

    pub_armor = peer_data.get("public_key") or peer_data.get("public_key_armor")
    if not pub_armor:
        return _json_response({
            "name": name,
            "verified": False,
            "cached_did_key": peer_data.get("did_key"),
            "detail": "No public_key in peer file — cannot compute did:key",
        })

    try:
        from capauth.did import (  # type: ignore[import]
            _compute_did_key,
            _pgp_armor_to_rsa_numbers,
            _rsa_numbers_to_der,
        )
        n, e = _pgp_armor_to_rsa_numbers(pub_armor)
        computed = _compute_did_key(_rsa_numbers_to_der(n, e))
    except Exception as exc:
        return _error_response(f"DID computation failed: {exc}")

    cached = peer_data.get("did_key")
    match = (cached == computed) if cached else None

    # Cache computed did:key back to peer file
    if not cached or not match:
        peer_data["did_key"] = computed
        try:
            peer_file.write_text(
                _json.dumps(peer_data, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:
            pass

    return _json_response({
        "name": name,
        "fingerprint": peer_data.get("fingerprint"),
        "computed_did_key": computed,
        "cached_did_key": cached,
        "match": match,
        "verified": True,
        "detail": (
            "did:key computed from public key"
            + (" (matches cached)" if match else " (cache updated)")
        ),
    })


async def _handle_did_publish(args: dict) -> list[TextContent]:
    """Write DID tiers to disk, respecting publication policy."""
    hostname, tailnet = _resolve_tailnet(
        args.get("tailnet_hostname", ""),
        args.get("tailnet_name", ""),
    )
    org_domain = args.get("org_domain", "skworld.io")
    agent_slug = args.get("agent_slug", "")

    # Resolve publish_public: explicit arg overrides persisted policy
    policy = _load_policy()
    if "publish_public" in args:
        publish_public = bool(args["publish_public"])
        policy["publish_public"] = publish_public
        _save_policy(policy)
    else:
        publish_public = bool(policy.get("publish_public", True))

    try:
        from capauth.did import DIDDocumentGenerator, DIDTier  # type: ignore[import]
    except ImportError as exc:
        return _error_response(f"capauth.did not available: {exc}")

    try:
        gen = DIDDocumentGenerator.from_profile()
    except Exception as exc:
        return _error_response(f"Could not load CapAuth profile: {exc}")

    docs = gen.generate_all(
        tailnet_hostname=hostname,
        tailnet_name=tailnet,
        org_domain=org_domain,
        agent_slug=agent_slug,
    )

    written: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    def _write(path: Path, content: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            written.append(str(path))
        except Exception as exc:
            errors.append(f"{path}: {exc}")

    skcomm_home = Path(_os.environ.get("SKCOMM_HOME", str(Path.home() / ".skcomm")))
    did_dir = _home() / "did"

    # Tier 2 (mesh) — always written; used by Tailscale Serve
    _write(skcomm_home / "well-known" / "did.json", _json.dumps(docs[DIDTier.WEB_MESH], indent=2))
    # Tier 1 (did:key) — always written; self-contained anchor
    _write(did_dir / "key.json", _json.dumps(docs[DIDTier.KEY], indent=2))
    _write(did_dir / "did_key.txt", gen._ctx.did_key_id)

    # Tier 3 (public) — only written when opt-in (default)
    if publish_public:
        _write(did_dir / "public.json", _json.dumps(docs[DIDTier.WEB_PUBLIC], indent=2))
    else:
        skipped.append(str(did_dir / "public.json"))

    return _json_response({
        "published": not errors,
        "publish_public": publish_public,
        "did_key": gen._ctx.did_key_id,
        "fingerprint": gen._ctx.fingerprint,
        "written": written,
        "skipped": skipped,
        "errors": errors,
        "note": (
            None if publish_public
            else "Tier 3 (public) skipped — opt-out active. Run did_publish(publish_public=true) to enable."
        ),
    })


async def _handle_did_policy(args: dict) -> list[TextContent]:
    """View or update DID publication policy."""
    policy = _load_policy()

    if "publish_public" in args:
        policy["publish_public"] = bool(args["publish_public"])
        _save_policy(policy)
        action = "updated"
    else:
        action = "viewed"

    publish_public = bool(policy.get("publish_public", True))

    return _json_response({
        "action": action,
        "publish_public": publish_public,
        "policy_file": str(_policy_path()),
        "privacy_level": (
            "public — Tier 1 (did:key) + Tier 2 (mesh) + Tier 3 (skworld.io)"
            if publish_public
            else "private — Tier 1 (did:key) + Tier 2 (mesh) only; no public internet exposure"
        ),
        "note": (
            "Default. To opt out: did_policy(publish_public=false)"
            if publish_public
            else "Opted out of public publishing. To opt back in: did_policy(publish_public=true)"
        ),
    })


async def _handle_did_identity_card(args: dict) -> list[TextContent]:
    """Generate a full sovereign identity card."""
    include_soul = bool(args.get("include_soul", True))

    try:
        from capauth.did import DIDDocumentGenerator  # type: ignore[import]
    except ImportError as exc:
        return _error_response(f"capauth.did not available: {exc}")

    try:
        gen = DIDDocumentGenerator.from_profile()
    except Exception as exc:
        return _error_response(f"Could not load CapAuth profile: {exc}")

    card = gen.generate_identity_card(include_soul=include_soul)
    return _json_response(card)


HANDLERS: dict = {
    "did_show": _handle_did_show,
    "did_verify_peer": _handle_did_verify_peer,
    "did_publish": _handle_did_publish,
    "did_policy": _handle_did_policy,
    "did_identity_card": _handle_did_identity_card,
}
