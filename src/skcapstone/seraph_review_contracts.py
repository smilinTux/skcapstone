"""Typed trust-boundary inputs for Seraph GitHub review publication."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Protocol

SUPPORTED_REPOSITORIES = frozenset(
    {"smilinTux/skcapstone", "smilinTux/skdashboard", "smilinTux/skworld"}
)
SERAPH_CAPAUTH_URI = "capauth:seraph@skworld.io"
REVIEW_PERMISSIONS = frozenset({"metadata:read", "contents:read", "pull_requests:write"})
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PASS = re.compile(r"^\s*PASS(?:\b|$)", re.IGNORECASE)


class ReviewPublicationError(ValueError):
    """Raised when publication cannot be proven safe and current."""


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewPublicationError(f"{label}_missing")
    return value.strip()


def _identity(value: object, label: str) -> str:
    return _text(value, f"{label}_identity").lower()


@dataclass(frozen=True)
class AuthenticatedCaller:
    """Opaque CapAuth presentation supplied for authoritative verification."""

    presentation: object


@dataclass(frozen=True)
class VerifiedCaller:
    """Authoritative CapAuth result bound to one publication request."""

    principal_uri: str
    capabilities: frozenset[str]
    request_sha256: str


class CapAuthVerifier(Protocol):
    """Trusted verification boundary for an opaque CapAuth presentation."""

    def verify(
        self,
        presentation: object,
        *,
        required_principal: str,
        required_capability: str,
        request_sha256: str,
    ) -> VerifiedCaller:
        """Verify identity and capability, bound to the exact request digest."""


@dataclass(frozen=True)
class ConnectorCapabilities:
    """Authenticated GitHub service identity and exact repository scopes."""

    service_identity: str
    permissions: frozenset[str]

    def validate(self, expected_identity: str) -> None:
        if _identity(self.service_identity, "service") != expected_identity:
            raise ReviewPublicationError("connector_service_identity_mismatch")
        if not isinstance(self.permissions, frozenset):
            raise ReviewPublicationError("connector_permissions_malformed")
        if self.permissions != REVIEW_PERMISSIONS:
            raise ReviewPublicationError("connector_permissions_not_least_privilege")


@dataclass(frozen=True)
class BranchProtection:
    """Hash-sealed required-check policy from a trusted injected reader."""

    repository: str
    base_branch: str
    required_checks: frozenset[str]
    policy_revision: str

    def _payload(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "base_branch": self.base_branch,
            "required_checks": sorted(self.required_checks),
        }

    def validate(self) -> None:
        if self.repository not in SUPPORTED_REPOSITORIES:
            raise ReviewPublicationError("unsupported_repository")
        _text(self.base_branch, "base_branch")
        if not isinstance(self.required_checks, frozenset) or not self.required_checks:
            raise ReviewPublicationError("required_checks_missing")
        if any(not isinstance(name, str) or not name.strip() for name in self.required_checks):
            raise ReviewPublicationError("required_checks_malformed")
        if not isinstance(self.policy_revision, str) or not _SHA256.fullmatch(
            self.policy_revision
        ):
            raise ReviewPublicationError("branch_protection_revision_invalid")
        if self.policy_revision != _digest(self._payload()):
            raise ReviewPublicationError("branch_protection_revision_mismatch")


class BranchProtectionReader(Protocol):
    """Trusted branch-protection read boundary supplied by the caller."""

    def read(self, repository: str, base_branch: str) -> BranchProtection:
        """Return hash-sealed required checks for the protected branch."""


@dataclass(frozen=True)
class LivePullRequest:
    """Minimal live GitHub state needed by the publisher."""

    repository: str
    number: int
    head_sha: str
    base_branch: str
    author: str
    checks: Mapping[str, str]

    def validate(self) -> None:
        if self.repository not in SUPPORTED_REPOSITORIES:
            raise ReviewPublicationError("unsupported_repository")
        if (
            self.number < 1
            or not isinstance(self.head_sha, str)
            or not _GIT_SHA.fullmatch(self.head_sha)
        ):
            raise ReviewPublicationError("invalid_live_pull_request")
        _text(self.base_branch, "base_branch")
        _text(self.author, "author")
        if not isinstance(self.checks, Mapping) or any(
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(state, str)
            or not state.strip()
            for name, state in self.checks.items()
        ):
            raise ReviewPublicationError("checks_malformed")


@dataclass(frozen=True)
class CardSnapshot:
    """Current CardStore facts relevant to review publication."""

    card_id: str
    revision: str
    status: str
    verdict: str | None
    parent_id: str | None
    evidence_path: str | None = None
    evidence_sha256: str | None = None


class CardStoreGateway(Protocol):
    """Live CardStore read and append-only receipt boundary."""

    def read_card(self, card_id: str) -> CardSnapshot:
        """Read one current card snapshot or fail if it does not exist."""

    def append_publication_receipt(
        self,
        *,
        review_card: str,
        transition_id: str,
        receipt_path: Path,
        receipt_sha256: str,
        github_review_id: str,
        head_sha: str,
    ) -> str:
        """Append and read back one idempotent publication receipt event."""


@dataclass(frozen=True)
class SeraphPassEvidence:
    """Caller request identifying one independently recorded Seraph review."""

    repository: str
    number: int
    head_sha: str
    source_card: str
    source_card_revision: str
    review_card: str
    review_card_revision: str
    reviewer_identity: str
    verdict: str
    evidence_sha256: str

    def validate(self) -> None:
        if self.repository not in SUPPORTED_REPOSITORIES:
            raise ReviewPublicationError("unsupported_repository")
        if (
            self.number < 1
            or not isinstance(self.head_sha, str)
            or not _GIT_SHA.fullmatch(self.head_sha)
        ):
            raise ReviewPublicationError("invalid_evidence_identity")
        for value, label in (
            (self.source_card, "source_card"),
            (self.review_card, "review_card"),
            (self.reviewer_identity, "reviewer"),
            (self.verdict, "verdict"),
        ):
            _text(value, label)
        if self.source_card == self.review_card:
            raise ReviewPublicationError("source_and_review_card_must_differ")
        if (
            not isinstance(self.source_card_revision, str)
            or not isinstance(self.review_card_revision, str)
            or not _SHA256.fullmatch(self.source_card_revision)
            or not _SHA256.fullmatch(self.review_card_revision)
        ):
            raise ReviewPublicationError("invalid_card_revision")
        if _identity(self.reviewer_identity, "reviewer") != SERAPH_CAPAUTH_URI:
            raise ReviewPublicationError("reviewer_is_not_seraph")
        if not _PASS.fullmatch(self.verdict):
            raise ReviewPublicationError("verdict_is_not_pass")
        if not isinstance(self.evidence_sha256, str) or not _SHA256.fullmatch(
            self.evidence_sha256
        ):
            raise ReviewPublicationError("invalid_evidence_hash")


@dataclass(frozen=True)
class PublishedReview:
    """An existing exact-head GitHub approval."""

    review_id: str
    repository: str
    number: int
    commit_sha: str
    reviewer_identity: str
    event: str


class GitHubReviewConnector(Protocol):
    """Only the GitHub reads and review write required by this operation."""

    def capabilities(self) -> ConnectorCapabilities:
        """Return authenticated identity and effective repository permissions."""

    def read_pull_request(self, repository: str, number: int) -> LivePullRequest:
        """Read current PR identity, head, base, author, and check conclusions."""

    def find_approval(
        self, repository: str, number: int, *, service_identity: str, head_sha: str
    ) -> PublishedReview | None:
        """Find the service identity's current exact-head approval."""

    def create_review(
        self,
        repository: str,
        number: int,
        *,
        commit_sha: str,
        event: str,
        service_identity: str,
        idempotency_key: str,
    ) -> None:
        """Create one approval; implementation owns its external credential."""


@dataclass(frozen=True)
class PublicationReceipt:
    """Hash-sealed, append-once publication record."""

    schema: str
    idempotency_key: str
    repository: str
    number: int
    head_sha: str
    source_card: str
    source_card_revision: str
    review_card: str
    review_card_revision: str
    evidence_sha256: str
    reviewer_identity: str
    service_identity: str
    github_review_id: str
    published_at: str
    receipt_sha256: str

    def _payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload.pop("receipt_sha256")
        return payload

    def validate(self) -> None:
        if self.schema != "skfleet.seraph-review-publication/v1":
            raise ReviewPublicationError("receipt_schema_mismatch")
        if not isinstance(self.receipt_sha256, str) or not _SHA256.fullmatch(self.receipt_sha256):
            raise ReviewPublicationError("receipt_identity_invalid")
        if self.receipt_sha256 != _digest(self._payload()):
            raise ReviewPublicationError("receipt_hash_mismatch")
