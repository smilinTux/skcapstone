"""Signed, single-use human authorization envelopes for ATLAS mutations."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")


class AuthorizationError(ValueError):
    """An authorization envelope is invalid, expired, or already consumed."""


class AuthorizationEnvelope(BaseModel):
    """A detached signature over one exact operational mutation."""

    schema_version: str = "atlas-authorization/v1"
    authorization_id: str
    issuer: str
    issuer_role: str
    issuer_fingerprint: str
    action: str
    target: str
    change_id: str
    scope: str
    issued_at: str
    expires_at: str
    nonce: str
    signature: str = ""

    def signing_bytes(self) -> bytes:
        """Return deterministic bytes excluding the detached signature."""
        body = self.model_dump(exclude={"authorization_id", "signature"})
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def authorization_id(envelope: AuthorizationEnvelope) -> str:
    """Return the content-derived identifier for an envelope."""
    return "authz-" + hashlib.sha256(envelope.signing_bytes()).hexdigest()[:24]


def verify_authorization(
    envelope: AuthorizationEnvelope,
    *,
    public_key_armor: str,
    verifier: Callable[[bytes, str, str], bool],
    expected_action: str,
    expected_target: str,
    expected_change_id: str,
    expected_scope: str,
    now: datetime | None = None,
) -> None:
    """Verify identity, signature, lifetime, and exact mutation binding."""
    if envelope.schema_version != "atlas-authorization/v1":
        raise AuthorizationError("unsupported authorization schema")
    if envelope.issuer_role not in {"owner", "approver"}:
        raise AuthorizationError("authorization issuer is not a human approver")
    if not _NONCE_RE.fullmatch(envelope.nonce):
        raise AuthorizationError("authorization nonce is malformed")
    expected = (expected_action, expected_target, expected_change_id, expected_scope)
    actual = (envelope.action, envelope.target, envelope.change_id, envelope.scope)
    if actual != expected:
        raise AuthorizationError("authorization does not match action, target, change, and scope")
    try:
        issued = datetime.fromisoformat(envelope.issued_at.replace("Z", "+00:00"))
        expires = datetime.fromisoformat(envelope.expires_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorizationError("authorization lifetime is malformed") from exc
    current = now or datetime.now(timezone.utc)
    if issued.tzinfo is None or expires.tzinfo is None or not issued <= current < expires:
        raise AuthorizationError("authorization is not currently valid")
    if envelope.authorization_id != authorization_id(envelope):
        raise AuthorizationError("authorization content identifier mismatch")
    if not envelope.signature or not verifier(
        envelope.signing_bytes(), envelope.signature, public_key_armor
    ):
        raise AuthorizationError("authorization signature is invalid")


def consume_authorization(envelope: AuthorizationEnvelope, store: Path) -> None:
    """Atomically consume an authorization nonce, rejecting replay."""
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = store / f"{envelope.nonce}.used"
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise AuthorizationError("authorization has already been consumed") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(envelope.authorization_id + "\n")


def load_authorization(path: Path) -> AuthorizationEnvelope:
    """Load one bounded authorization envelope from disk."""
    try:
        return AuthorizationEnvelope.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuthorizationError(f"cannot load authorization: {exc}") from exc
