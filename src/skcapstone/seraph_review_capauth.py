"""Request-bound CapAuth verification for private Forgejo review publication."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .fleet.operator_http import AUTH_SKEW_S, verify_signed_request
from .fleet.signing import roster_by_fingerprint
from .seraph_review_contracts import ReviewPublicationError, VerifiedCaller

SERAPH_FINGERPRINT = "FB3C247D1378A222956ACD080F8F9DA4D73DC34E"
SERAPH_PRINCIPAL = "capauth:seraph@skworld.io"
PUBLISH_CAPABILITY = "forge-review:publish"
PUBLISH_METHOD = "POST"
PUBLISH_PATH = "/skfleet/v1/seraph-review"
LEGACY_GRACE_ENV = "CAPAUTH_LEGACY_UNSIGNED_GRACE_UNTIL"


class DurableNonceCache:
    """Reserve signed request nonces atomically across process restarts."""

    def __init__(self, path: Path, window_s: float = AUTH_SKEW_S) -> None:
        self.path, self.window_s = path, window_s
        self._lock = threading.Lock()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.stat().st_mode & 0o077:
            raise ReviewPublicationError("capauth_replay_directory_permissions_invalid")
        if path.exists() and (path.is_symlink() or not stat.S_ISREG(path.stat().st_mode)):
            raise ReviewPublicationError("capauth_replay_store_invalid")
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS nonce "
                "(fingerprint TEXT NOT NULL, nonce TEXT NOT NULL, "
                "seen_at REAL NOT NULL, PRIMARY KEY (fingerprint, nonce))"
            )
        os.chmod(path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10, isolation_level=None)

    def check_and_record(self, fingerprint: str, nonce: str, now: float) -> bool:
        """Commit one unique nonce or return False when it was already used."""
        with self._lock, self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM nonce WHERE seen_at < ?", (now - self.window_s,))
                connection.execute("INSERT INTO nonce VALUES (?, ?, ?)", (fingerprint, nonce, now))
                connection.execute("COMMIT")
                return True
            except sqlite3.IntegrityError:
                connection.execute("ROLLBACK")
                return False


class JsonlAuditSink:
    """Append sanitized authorization decisions durably before an allow returns."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.stat().st_mode & 0o077:
            raise ReviewPublicationError("capauth_audit_directory_permissions_invalid")
        if path.exists() and (path.is_symlink() or not stat.S_ISREG(path.stat().st_mode)):
            raise ReviewPublicationError("capauth_audit_store_invalid")

    def write(self, record: Mapping[str, Any]) -> None:
        """Write one canonical record and fsync it before returning."""
        raw = json.dumps(dict(record), sort_keys=True, separators=(",", ":")) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            os.chmod(self.path, 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())


def _rules() -> dict[str, Any]:
    from capauth import CapabilityRule
    from capauth.pairing import EnrollmentMode

    return {
        PUBLISH_CAPABILITY: CapabilityRule(
            capability=PUBLISH_CAPABILITY,
            required_capability=PUBLISH_CAPABILITY,
            minimum_mode=EnrollmentMode.VERIFIED,
            description="Publish one independently reviewed exact-head Forgejo approval.",
        )
    }


class SeraphCapAuthVerifier:
    """Authenticate Seraph and authorize one exact publication request digest."""

    def __init__(
        self,
        *,
        base_dir: Path,
        nonce_store: DurableNonceCache,
        audit_sink: JsonlAuditSink,
        roster: Callable[[], dict[str, str]] = roster_by_fingerprint,
        verify_request: Callable[..., Any] = verify_signed_request,
        decide: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.base_dir = base_dir
        self.nonce_store = nonce_store
        self.audit_sink = audit_sink
        self.roster = roster
        self.verify_request = verify_request
        self.decide = decide
        self.clock = clock

    def verify(
        self,
        presentation: object,
        *,
        required_principal: str,
        required_capability: str,
        request_sha256: str,
    ) -> VerifiedCaller:
        """Verify signed request, fixed identity, PDP grant, audit, and exact digest."""
        if required_principal != SERAPH_PRINCIPAL or required_capability != PUBLISH_CAPABILITY:
            raise ReviewPublicationError("capauth_request_not_authorized")
        if os.environ.get(LEGACY_GRACE_ENV):
            raise ReviewPublicationError("capauth_legacy_grace_forbidden")
        if not isinstance(presentation, Mapping) or set(presentation) != {"headers"}:
            raise ReviewPublicationError("capauth_presentation_invalid")
        headers = presentation["headers"]
        if not isinstance(headers, Mapping):
            raise ReviewPublicationError("capauth_presentation_invalid")
        body = request_sha256.encode("ascii")
        result = self.verify_request(
            PUBLISH_METHOD,
            PUBLISH_PATH,
            body,
            headers,
            roster_by_fpr=self.roster,
            nonce_cache=self.nonce_store,
        )
        if result.reason is not None or result.fingerprint != SERAPH_FINGERPRINT:
            raise ReviewPublicationError("capauth_signed_request_invalid")
        decide = self.decide
        if decide is None:
            from capauth import decide as capauth_decide

            decide = capauth_decide
        decision = decide(
            SERAPH_PRINCIPAL,
            PUBLISH_CAPABILITY,
            resource={"request_sha256": request_sha256},
            context={"fingerprint": SERAPH_FINGERPRINT},
            base_dir=self.base_dir,
            rules=_rules(),
        )
        reason = str(getattr(decision, "reason", ""))
        obligations = getattr(decision, "obligations", None)
        if (
            getattr(decision, "allow", False) is not True
            or "LEGACY GRACE" in reason.upper()
            or not isinstance(obligations, list)
            or not any(getattr(item, "kind", None) == "audit" for item in obligations)
        ):
            raise ReviewPublicationError("capauth_capability_denied")
        moment = self.clock()
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ReviewPublicationError("capauth_audit_clock_invalid")
        self.audit_sink.write(
            {
                "schema": "skfleet.seraph-review-authorization/v1",
                "at": moment.astimezone(timezone.utc).isoformat(),
                "subject": SERAPH_PRINCIPAL,
                "fingerprint": SERAPH_FINGERPRINT,
                "capability": PUBLISH_CAPABILITY,
                "request_sha256": request_sha256,
                "allow": True,
                "reason": reason,
            }
        )
        return VerifiedCaller(SERAPH_PRINCIPAL, frozenset({PUBLISH_CAPABILITY}), request_sha256)
