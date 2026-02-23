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

    try:
        from capauth.keys import generate_keypair  # type: ignore[import-untyped]

        pub_key, fingerprint = generate_keypair(
            name=name,
            email=state.email,
            output_dir=str(identity_dir),
        )
        state.fingerprint = fingerprint
        state.key_path = identity_dir / "agent.pub"
        state.status = PillarStatus.ACTIVE
    except (ImportError, Exception):
        # Reason: CapAuth not installed or key generation failed —
        # record identity metadata anyway so agent has a name
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
