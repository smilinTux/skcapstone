"""Human-operator link helpers for manifests and identity attestations."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def discover_human_operator(capauth_home: Path | None = None) -> dict[str, str] | None:
    """Return the active human operator from the local CapAuth profile.

    Args:
        capauth_home: Optional CapAuth home directory. Defaults to ``~/.capauth``.

    Returns:
        A compact operator mapping, or ``None`` if no human profile is available.
    """
    base = Path(capauth_home).expanduser() if capauth_home else _resolve_operator_home()
    profile_path = base / "identity" / "profile.json"
    if not profile_path.exists():
        return None

    try:
        data = json.loads(profile_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    entity = data.get("entity", {})
    key_info = data.get("key_info", {})
    entity_type = str(entity.get("entity_type", "")).lower()
    if entity_type not in {"human", "entitytype.human"}:
        return None

    name = entity.get("name", "").strip()
    if not name:
        return None

    operator = {
        "name": name,
        "relationship": "human-operator",
        "entity_type": "human",
        "source": "capauth",
    }
    if entity.get("email"):
        operator["email"] = entity["email"]
    if entity.get("handle"):
        operator["handle"] = entity["handle"]
    if key_info.get("fingerprint"):
        operator["fingerprint"] = key_info["fingerprint"]
    return operator


def build_agent_manifest(
    name: str,
    version: str,
    *,
    created_at: str | None = None,
    connectors: list[str] | None = None,
    operator: dict[str, str] | None = None,
    entity_type: str = "ai-agent",
) -> dict[str, Any]:
    """Build a standard manifest for a sovereign agent."""
    manifest: dict[str, Any] = {
        "name": name,
        "version": version,
        "entity_type": entity_type,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "connectors": connectors or [],
    }
    if operator:
        manifest["operator"] = operator
    return manifest


def create_operator_attestation(
    agent_name: str,
    agent_fingerprint: str,
    agent_public_key_path: Path,
    output_dir: Path,
    *,
    capauth_home: Path | None = None,
) -> dict[str, Any] | None:
    """Create a signed attestation linking a human operator to an agent key.

    The operator remains distinct from the agent identity. This produces a
    signed claim that the human operator vouches for the agent fingerprint.

    Args:
        agent_name: Agent display name.
        agent_fingerprint: Agent PGP fingerprint.
        agent_public_key_path: Path to the agent public key armor.
        output_dir: Directory where the attestation JSON should be written.
        capauth_home: Optional CapAuth home for the human operator.

    Returns:
        The attestation mapping, or ``None`` if no human operator profile is
        available or signing failed.
    """
    base = Path(capauth_home).expanduser() if capauth_home else _resolve_operator_home()
    profile_path = base / "identity" / "profile.json"
    private_key_path = base / "identity" / "private.asc"
    public_key_path = base / "identity" / "public.asc"
    if not profile_path.exists() or not private_key_path.exists() or not public_key_path.exists():
        return None

    try:
        from capauth.crypto import get_backend  # type: ignore[import-untyped]
        from capauth.profile import load_profile  # type: ignore[import-untyped]
    except ImportError:
        return None

    try:
        profile = load_profile(base_dir=base)
    except Exception as e:
        logger.warning("operator_link.py: %s", e)
        return None

    # The operator's two halves must be ONE key, or this attestation is signed
    # by something other than the identity it names. Unlike GTD/fleet signing
    # (which #115 repoints at the acting agent's home) this signer cannot move:
    # an operator attestation is the human-authorizes-agent link, so the
    # operator's key is the correct signer by definition. Found on noroc2027,
    # where the operator home held a stray `test-agent` private key beside the
    # real operator public key: any attestation minted there would have been
    # well-formed, signed by a test key, and unverifiable against the published
    # operator key. Refuse rather than mint an authorization grant nobody can
    # check. See capauth's keypair_match doctor check.
    if not _keypair_matches(private_key_path, public_key_path):
        logger.error(
            "operator_link.py: refusing to sign an attestation: the operator's "
            "private key (%s) and public key (%s) at %s are different keys",
            _fingerprint_of(private_key_path) or "unreadable",
            _fingerprint_of(public_key_path) or "unreadable",
            base / "identity",
        )
        return None

    entity_type = str(profile.entity.entity_type).lower()
    if entity_type not in {"human", "entitytype.human"}:
        return None

    try:
        payload = {
            "agent_name": agent_name,
            "agent_fingerprint": agent_fingerprint,
            "agent_public_key_path": str(agent_public_key_path),
            "relationship": "human-operator",
            "operator_name": profile.entity.name,
            "operator_email": profile.entity.email,
            "operator_handle": profile.entity.handle,
            "operator_fingerprint": profile.key_info.fingerprint,
            "signed_at": datetime.now(timezone.utc).isoformat(),
        }
        payload_bytes = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        private_armor = private_key_path.read_text(encoding="utf-8")
        operator_public_armor = public_key_path.read_text(encoding="utf-8")
        backend = get_backend(profile.crypto_backend)
        signature = backend.sign(payload_bytes, private_armor, "")
    except Exception as e:
        logger.warning("operator_link.py: %s", e)
        return None

    attestation = {
        "payload": payload,
        "signature": signature,
        "operator_public_key_path": str(public_key_path),
        "operator_public_key_armor": operator_public_armor,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "operator-attestation.json").write_text(
        json.dumps(attestation, indent=2),
        encoding="utf-8",
    )
    return attestation


def _fingerprint_of(path: Path) -> str | None:
    """The fingerprint of a PGP key file, or None when unreadable.

    Only the fingerprint (public material, derived from the primary key packet)
    is read; the key is never unlocked and no secret bytes are surfaced.
    """
    try:
        import pgpy  # type: ignore[import-untyped]

        key, _ = pgpy.PGPKey.from_file(str(path))
        return str(key.fingerprint).replace(" ", "")
    except Exception:  # noqa: BLE001
        return None


def _keypair_matches(private_key_path: Path, public_key_path: Path) -> bool:
    """True when the two files are halves of the SAME key.

    Unknown (PGPy unavailable, or either file unreadable) counts as a match:
    this guard exists to refuse a demonstrably wrong pair, not to block signing
    wherever the check cannot run.
    """
    priv = _fingerprint_of(private_key_path)
    pub = _fingerprint_of(public_key_path)
    if priv is None or pub is None:
        return True
    return priv == pub


def _resolve_operator_home() -> Path:
    """Resolve the human operator's CapAuth home."""
    try:
        from capauth import resolve_capauth_home  # type: ignore[import-untyped]

        return resolve_capauth_home()
    except Exception as e:
        logger.warning("operator_link.py: %s", e)
        return Path.home() / ".skcapstone" / "capauth"
