"""
Identity pillar — CapAuth integration.

PGP-based sovereign identity. The agent IS its key.
No corporate SSO. No OAuth dance. Cryptographic proof of self.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..models import IdentityState, PillarStatus


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

    capauth_state = _try_init_capauth(name, state.email, identity_dir)
    if capauth_state is not None:
        state.fingerprint = capauth_state.fingerprint
        state.key_path = capauth_state.key_path
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
    (identity_dir / "identity.json").write_text(json.dumps(identity_manifest, indent=2))

    return state


def _try_init_capauth(
    name: str, email: str, identity_dir: Path
) -> Optional[IdentityState]:
    """Try to create or load a real CapAuth identity.

    Attempts (in order):
    1. Load an existing CapAuth profile from ~/.capauth/
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

        profile = load_profile()
        return IdentityState(
            fingerprint=profile.key_info.fingerprint,
            key_path=Path(profile.key_info.public_key_path),
            name=profile.entity.name,
            email=profile.entity.email,
            status=PillarStatus.ACTIVE,
        )
    except ImportError:
        return None
    except Exception:
        pass

    # No existing profile — try creating one
    try:
        from capauth.profile import init_profile  # type: ignore[import-untyped]

        profile = init_profile(
            name=name,
            email=email,
            passphrase="",
        )
        return IdentityState(
            fingerprint=profile.key_info.fingerprint,
            key_path=Path(profile.key_info.public_key_path),
            name=profile.entity.name,
            email=profile.entity.email,
            status=PillarStatus.ACTIVE,
        )
    except Exception:
        pass

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
    except Exception:
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
