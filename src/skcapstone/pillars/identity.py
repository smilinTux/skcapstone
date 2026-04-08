"""
Identity pillar — CapAuth integration.

PGP-based sovereign identity. The agent IS its key.
No corporate SSO. No OAuth dance. Cryptographic proof of self.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from shutil import copyfile
from pathlib import Path
from typing import Optional

from ..models import IdentityState, PillarStatus
from ..operator_link import create_operator_attestation

logger = logging.getLogger("skcapstone.identity")


def _capauth_home(home: Path) -> Path:
    """Return the agent-local CapAuth home for an SKCapstone agent."""
    return home / "capauth"


def generate_identity(
    home: Path,
    name: str,
    email: Optional[str] = None,
) -> IdentityState:
    """Generate a new sovereign identity for the agent.

    Tries CapAuth first (full PGP key generation).
    Falls back to recording identity metadata without key material.

    Args:
        home: Agent home directory (~/.skcapstone).
        name: Agent display name.
        email: Optional email for the identity.

    Returns:
        IdentityState with the new identity.
    """
    identity_dir = home / "identity"
    identity_dir.mkdir(parents=True, exist_ok=True)

    state = IdentityState(
        name=name,
        email=email or f"{name.lower().replace(' ', '-')}@skcapstone.local",
        created_at=datetime.now(timezone.utc),
        status=PillarStatus.DEGRADED,
    )

    capauth_home = _capauth_home(home)
    capauth_state = _try_init_capauth(name, state.email, identity_dir, capauth_home)
    if capauth_state is not None:
        state.fingerprint = capauth_state.fingerprint
        state.key_path = capauth_state.key_path
        state.name = capauth_state.name
        state.email = capauth_state.email
        state.created_at = capauth_state.created_at
        state.status = PillarStatus.ACTIVE
    else:
        state.fingerprint = _generate_placeholder_fingerprint(name)
        state.status = PillarStatus.DEGRADED

    identity_manifest = {
        "name": state.name,
        "email": state.email,
        "fingerprint": state.fingerprint,
        "created_at": state.created_at.isoformat() if state.created_at else None,
        "capauth_managed": state.status == PillarStatus.ACTIVE,
    }
    if state.key_path is not None:
        identity_manifest["public_key_path"] = str(state.key_path)
    if state.status == PillarStatus.ACTIVE:
        identity_manifest["capauth_home"] = str(capauth_home)

        attestation = create_operator_attestation(
            agent_name=state.name or name,
            agent_fingerprint=state.fingerprint or "",
            agent_public_key_path=state.key_path or (capauth_home / "identity" / "public.asc"),
            output_dir=identity_dir,
        )
        if attestation is not None:
            payload = attestation.get("payload", {})
            identity_manifest["operator_attestation_path"] = str(
                identity_dir / "operator-attestation.json"
            )
            identity_manifest["operator_attested_by"] = payload.get("operator_fingerprint")

    (identity_dir / "identity.json").write_text(json.dumps(identity_manifest, indent=2), encoding="utf-8")

    return state


def _try_init_capauth(
    name: str,
    email: str,
    identity_dir: Path,
    capauth_home: Path,
) -> Optional[IdentityState]:
    """Try to create or load a real CapAuth identity.

    Attempts (in order):
    1. Load an existing CapAuth profile from the agent-local CapAuth home
    2. Create a new profile via capauth.profile.init_profile()
    3. Fall back to legacy capauth.keys.generate_keypair()

    Args:
        name: Agent display name.
        email: Agent email.
        identity_dir: Path to ~/.skcapstone/identity/.

    Returns:
        IdentityState with real PGP keys, or None if CapAuth unavailable.
    """
    # Try loading an existing CapAuth profile first
    try:
        from capauth.profile import load_profile  # type: ignore[import-untyped]

        profile = load_profile(base_dir=capauth_home)
        key_path = Path(profile.key_info.public_key_path)
        legacy_key_path = identity_dir / "agent.pub"
        if key_path.exists() and not legacy_key_path.exists():
            copyfile(key_path, legacy_key_path)
        return IdentityState(
            fingerprint=profile.key_info.fingerprint,
            key_path=key_path,
            name=profile.entity.name,
            email=profile.entity.email,
            created_at=profile.key_info.created,
            status=PillarStatus.ACTIVE,
        )
    except ImportError:
        return None
    except Exception as exc:
        logger.debug("Could not load existing CapAuth profile: %s", exc)

    # No existing profile — try creating one
    try:
        from capauth.models import EntityType  # type: ignore[import-untyped]
        from capauth.profile import init_profile  # type: ignore[import-untyped]

        profile = init_profile(
            name=name,
            email=email,
            passphrase="",
            entity_type=EntityType.AI,
            base_dir=capauth_home,
        )
        key_path = Path(profile.key_info.public_key_path)
        legacy_key_path = identity_dir / "agent.pub"
        if key_path.exists():
            copyfile(key_path, legacy_key_path)
        return IdentityState(
            fingerprint=profile.key_info.fingerprint,
            key_path=key_path,
            name=profile.entity.name,
            email=profile.entity.email,
            created_at=profile.key_info.created,
            status=PillarStatus.ACTIVE,
        )
    except Exception as exc:
        logger.debug("Could not create CapAuth profile: %s", exc)

    # Legacy fallback: capauth.keys.generate_keypair
    try:
        from capauth.keys import generate_keypair  # type: ignore[import-untyped]

        _pub_key, fingerprint = generate_keypair(
            name=name,
            email=email,
            output_dir=str(identity_dir),
        )
        return IdentityState(
            fingerprint=fingerprint,
            key_path=identity_dir / "agent.pub",
            name=name,
            email=email,
            status=PillarStatus.ACTIVE,
        )
    except Exception as exc:
        logger.debug("CapAuth keypair generation failed: %s", exc)
        return None


def _generate_placeholder_fingerprint(name: str) -> str:
    """Generate a deterministic placeholder fingerprint from the agent name.

    Not cryptographic — just a consistent identifier until CapAuth
    is installed and proper PGP keys are generated.

    Args:
        name: Agent display name.

    Returns:
        A hex string resembling a fingerprint.
    """
    import hashlib

    digest = hashlib.sha256(f"skcapstone:{name}".encode()).hexdigest()
    return digest[:40].upper()
