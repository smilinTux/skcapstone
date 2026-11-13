"""Throwaway signed CAB grants for tests and the fault-injection drill (card bada5c86).

Never used for a real approval: the key is generated in memory, its passphrase is a
constant, and its fingerprint is pinned only in a scratch pin file that the caller
points ``cab_approval.APPROVERS_FILE`` (and skcoord's, when present) at.
"""

from __future__ import annotations

import functools
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PASSPHRASE = "throwaway-test-key"
#: The operator's real pin file: never written by this module.
_REAL_PIN = Path("~/.config/skcapstone/cab-approvers.json")


def key(name: str = "chef"):
    """An in-memory Ed25519 key for *name*, one per identity and process."""
    from .cab_approval import identity_key

    return _key(identity_key(name))


@functools.lru_cache(maxsize=None)
def _key(name: str):
    from capauth.crypto import get_backend
    from capauth.models import Algorithm

    return get_backend().generate_keypair(
        name, f"{name}@example.invalid", PASSPHRASE, algorithm=Algorithm.ED25519
    )


def write_pin(path: Path, identities=("chef",), roles=("owner", "approver")) -> Path:
    """Write a pin file trusting the throwaway keys of *identities*."""
    approvers = [
        {
            "identity": name,
            "fingerprint": key(name).fingerprint,
            "roles": list(roles),
            "backend": "pgpy",
            "public_key": key(name).public_armor,
        }
        for name in identities
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"approvers": approvers}), encoding="utf-8")
    return path


def ensure_pinned(name: str) -> None:
    """Add *name*'s throwaway key to the pin file the code currently reads.

    Test and drill use only: every pin target must already point at a scratch file.
    """
    from . import cab_approval

    path = Path(cab_approval.APPROVERS_FILE).expanduser()
    if path == _REAL_PIN.expanduser():
        raise RuntimeError("refusing to add a throwaway key to the operator's real pin file")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {"approvers": []}
    bundle = key(name)
    if any(e.get("fingerprint") == bundle.fingerprint for e in data["approvers"]):
        return
    data["approvers"].append(
        {
            "identity": name,
            "fingerprint": bundle.fingerprint,
            "roles": ["owner", "approver"],
            "backend": "pgpy",
            "public_key": bundle.public_armor,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def pin_targets() -> list[tuple[Any, str]]:
    """The module attributes that name the pin file (skcapstone's and skcoord's)."""
    from . import cab_approval

    targets: list[tuple[Any, str]] = [(cab_approval, "APPROVERS_FILE")]
    try:
        import skcoord.cab_authorization as skcoord_pin

        targets.append((skcoord_pin, "APPROVERS_FILE"))
    except ImportError:  # an skcoord release without signed-grant verification
        pass
    return targets


def envelope(
    change_id: str,
    issuer: str = "chef",
    role: str = "owner",
    decision: str = "approved",
    signer: str | None = None,
    target: str = "test-target",
    scope: str = "test-scope",
) -> dict:
    """A signed authorization envelope, as ``itil cab authorize`` writes one.

    The issuer is pinned (``ensure_pinned``) unless another *signer* is given, in
    which case neither is added: that is how a test forges an unpinned grant.
    """
    from capauth.crypto import get_backend

    from .operator_authorization import AuthorizationEnvelope, authorization_id

    if signer is None:
        ensure_pinned(issuer)
    bundle = key(signer or issuer)
    now = datetime.now(timezone.utc)
    env = AuthorizationEnvelope(
        authorization_id="pending",
        issuer=issuer,
        issuer_role=role,
        issuer_fingerprint=bundle.fingerprint,
        action=f"itil.cab.vote.{decision}",
        target=target,
        change_id=change_id,
        scope=scope,
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=10)).isoformat(),
        nonce=secrets.token_urlsafe(24),
    )
    env.authorization_id = authorization_id(env)
    env.signature = get_backend().sign(env.signing_bytes(), bundle.private_armor, PASSPHRASE)
    return env.model_dump()


def cast_signed_vote(
    itil: Any,
    change_id: str,
    subject: str = "chef",
    role: str = "owner",
    decision: str = "approved",
    signer: str | None = None,
):
    """Record a vote carrying a verifying grant, on any skcoord release."""
    from .cab_approval import record_signed_vote

    env = envelope(change_id, issuer=subject, role=role, decision=decision, signer=signer)
    return record_signed_vote(itil, env)


def decision_grant(
    decisions_dir: Any,
    decision_id: str,
    choice: int | None,
    subject: str,
    role: str = "approver",
) -> dict:
    """A signed approval grant for the change a parked decision's option links."""
    from .operator_seat.dispatch import decision_option

    _, change_id = decision_option(decisions_dir, decision_id, choice)
    return envelope(change_id, issuer=subject, role=role)
