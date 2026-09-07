"""Publish an exact Seraph PASS through narrow, injected trust boundaries."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import stat
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .seat_boundaries import Action, require_authority
from .seraph_review_cardstore import LiveCardStoreGateway
from .seraph_review_contracts import (
    _PASS,
    SERAPH_CAPAUTH_URI,
    AuthenticatedCaller,
    BranchProtection,
    BranchProtectionReader,
    CapAuthVerifier,
    CardSnapshot,
    CardStoreGateway,
    ConnectorCapabilities,
    GitHubReviewConnector,
    LivePullRequest,
    PublicationReceipt,
    PublishedReview,
    ReviewPublicationError,
    SeraphPassEvidence,
    VerifiedCaller,
    _digest,
    _identity,
)

SERAPH_REVIEW_CAPABILITY = "github-review:publish"


def _validate_cards(evidence: SeraphPassEvidence, cards: CardStoreGateway) -> CardSnapshot:
    source = cards.read_card(evidence.source_card)
    review = cards.read_card(evidence.review_card)
    if source.card_id != evidence.source_card or review.card_id != evidence.review_card:
        raise ReviewPublicationError("card_identity_mismatch")
    if (
        source.revision != evidence.source_card_revision
        or review.revision != evidence.review_card_revision
    ):
        raise ReviewPublicationError("card_revision_mismatch")
    if source.status not in {"review", "done"} or review.status != "done":
        raise ReviewPublicationError("card_status_not_publishable")
    if not isinstance(source.verdict, str) or not _PASS.match(source.verdict):
        raise ReviewPublicationError("source_card_verdict_not_pass")
    if not isinstance(review.verdict, str) or not _PASS.match(review.verdict):
        raise ReviewPublicationError("review_card_verdict_not_pass")
    if review.parent_id != source.card_id:
        raise ReviewPublicationError("review_parent_mismatch")
    return review


def _validate_evidence_artifact(
    evidence: SeraphPassEvidence,
    review: CardSnapshot,
    approved_root: Path,
) -> str:
    """Hash the regular, non-symlink artifact named by the live review card."""

    if not review.evidence_path or not review.evidence_sha256:
        raise ReviewPublicationError("card_evidence_missing")
    try:
        root = approved_root.resolve(strict=True)
        if not root.is_dir():
            raise ReviewPublicationError("evidence_root_invalid")
        candidate = Path(os.path.abspath(review.evidence_path))
        relative = candidate.relative_to(root)
        if not relative.parts:
            raise ReviewPublicationError("evidence_not_regular")
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in relative.parts[:-1]:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory,
                )
                os.close(directory)
                directory = child
            descriptor = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        finally:
            os.close(directory)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ReviewPublicationError("evidence_not_regular")
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
            after = os.fstat(descriptor)
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise ReviewPublicationError("evidence_changed_during_read")
        finally:
            os.close(descriptor)
    except ReviewPublicationError:
        raise
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ReviewPublicationError("evidence_symlink_rejected") from exc
        raise ReviewPublicationError("evidence_artifact_invalid") from exc
    except ValueError as exc:
        raise ReviewPublicationError("evidence_artifact_invalid") from exc
    actual = digest.hexdigest()
    if actual != review.evidence_sha256:
        raise ReviewPublicationError("evidence_artifact_hash_mismatch")
    if actual != evidence.evidence_sha256:
        raise ReviewPublicationError("caller_evidence_hash_mismatch")
    return actual


def _verify_caller(
    caller: AuthenticatedCaller,
    verifier: CapAuthVerifier,
    request_sha256: str,
) -> VerifiedCaller:
    """Require an authoritative exact-request Seraph capability decision."""

    try:
        verified = verifier.verify(
            caller.presentation,
            required_principal=SERAPH_CAPAUTH_URI,
            required_capability=SERAPH_REVIEW_CAPABILITY,
            request_sha256=request_sha256,
        )
    except ReviewPublicationError:
        raise
    except Exception as exc:
        raise ReviewPublicationError("capauth_verifier_unavailable") from exc
    if not isinstance(verified, VerifiedCaller):
        raise ReviewPublicationError("capauth_verification_invalid")
    if _identity(verified.principal_uri, "verified_caller") != SERAPH_CAPAUTH_URI:
        raise ReviewPublicationError("capauth_principal_mismatch")
    if verified.capabilities != frozenset({SERAPH_REVIEW_CAPABILITY}):
        raise ReviewPublicationError("capauth_capability_mismatch")
    if verified.request_sha256 != request_sha256:
        raise ReviewPublicationError("capauth_request_binding_mismatch")
    return verified


def _validate_review(
    review: PublishedReview | None,
    evidence: SeraphPassEvidence,
    service: str,
) -> PublishedReview:
    if review is None:
        raise ReviewPublicationError("github_review_missing")
    if (
        not isinstance(review.review_id, str)
        or not review.review_id.strip()
        or review.repository != evidence.repository
        or review.number != evidence.number
        or review.commit_sha != evidence.head_sha
        or not isinstance(review.event, str)
        or review.event.upper() != "APPROVE"
        or _identity(review.reviewer_identity, "published_review") != service
    ):
        raise ReviewPublicationError("github_review_mismatch")
    return review


def _validate_live(
    evidence: SeraphPassEvidence,
    *,
    service: str,
    connector: GitHubReviewConnector,
    cards: CardStoreGateway,
    protections: BranchProtectionReader,
    approved_evidence_root: Path,
) -> None:
    connector.capabilities().validate(service)
    review = _validate_cards(evidence, cards)
    _validate_evidence_artifact(evidence, review, approved_evidence_root)
    live = connector.read_pull_request(evidence.repository, evidence.number)
    live.validate()
    if live.repository != evidence.repository or live.number != evidence.number:
        raise ReviewPublicationError("live_pull_request_mismatch")
    if live.head_sha != evidence.head_sha:
        raise ReviewPublicationError("stale_head")
    policy = protections.read(evidence.repository, live.base_branch)
    policy.validate()
    if policy.repository != live.repository or policy.base_branch != live.base_branch:
        raise ReviewPublicationError("branch_protection_mismatch")
    if policy.required_checks.difference(live.checks):
        raise ReviewPublicationError("required_check_missing")
    if any(live.checks[name].strip().lower() != "success" for name in policy.required_checks):
        raise ReviewPublicationError("required_checks_not_terminal_green")
    if (
        len(
            {
                _identity(live.author, "author"),
                _identity(evidence.reviewer_identity, "reviewer"),
                service,
            }
        )
        != 3
    ):
        raise ReviewPublicationError("author_reviewer_service_not_distinct")


def _read_receipt(path: Path) -> PublicationReceipt:
    try:
        receipt = PublicationReceipt(**json.loads(path.read_text(encoding="utf-8")))
        receipt.validate()
        return receipt
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ReviewPublicationError("receipt_invalid") from exc


def _write_receipt(path: Path, receipt: PublicationReceipt) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(asdict(receipt), sort_keys=True, indent=2) + "\n").encode()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    except FileExistsError as exc:
        raise ReviewPublicationError("receipt_idempotency_conflict") from exc
    try:
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o444)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _receipt_matches(
    receipt: PublicationReceipt,
    evidence: SeraphPassEvidence,
    service: str,
    key: str,
) -> bool:
    return (
        receipt.idempotency_key == key
        and receipt.repository == evidence.repository
        and receipt.number == evidence.number
        and receipt.head_sha == evidence.head_sha
        and receipt.source_card == evidence.source_card
        and receipt.source_card_revision == evidence.source_card_revision
        and receipt.review_card == evidence.review_card
        and receipt.review_card_revision == evidence.review_card_revision
        and receipt.evidence_sha256 == evidence.evidence_sha256
        and receipt.reviewer_identity == evidence.reviewer_identity.lower()
        and receipt.service_identity == service
    )


def _record_cardstore_receipt(
    cards: CardStoreGateway,
    evidence: SeraphPassEvidence,
    receipt: PublicationReceipt,
    receipt_path: Path,
) -> None:
    cards.append_publication_receipt(
        review_card=evidence.review_card,
        transition_id=f"seraph-review-publication-{receipt.idempotency_key}",
        receipt_path=receipt_path,
        receipt_sha256=receipt.receipt_sha256,
        github_review_id=receipt.github_review_id,
        head_sha=evidence.head_sha,
    )


def publish_seraph_pass(
    evidence: SeraphPassEvidence,
    *,
    caller: AuthenticatedCaller,
    capauth: CapAuthVerifier,
    service_identity: str,
    connector: GitHubReviewConnector,
    cards: CardStoreGateway,
    protections: BranchProtectionReader,
    receipt_dir: Path,
    approved_evidence_root: Path,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> PublicationReceipt:
    """Publish one exact-head PASS and durably link its receipt to CardStore."""

    require_authority("seraph", Action.PUBLISH_REVIEW)
    evidence.validate()
    service = _identity(service_identity, "service")
    if service == SERAPH_CAPAUTH_URI:
        raise ReviewPublicationError("service_and_reviewer_must_differ")
    key = _digest(
        {
            "repository": evidence.repository,
            "number": evidence.number,
            "head_sha": evidence.head_sha,
            "source_card": evidence.source_card,
            "source_card_revision": evidence.source_card_revision,
            "review_card": evidence.review_card,
            "review_card_revision": evidence.review_card_revision,
            "evidence_sha256": evidence.evidence_sha256,
            "reviewer_identity": evidence.reviewer_identity.lower(),
            "service_identity": service,
        }
    )
    review = _validate_cards(evidence, cards)
    _validate_evidence_artifact(evidence, review, approved_evidence_root)
    _verify_caller(caller, capauth, key)
    receipt_path = receipt_dir / f"{key}.json"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    with (receipt_dir / f"{key}.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _validate_live(
            evidence,
            service=service,
            connector=connector,
            cards=cards,
            protections=protections,
            approved_evidence_root=approved_evidence_root,
        )
        existing = connector.find_approval(
            evidence.repository,
            evidence.number,
            service_identity=service,
            head_sha=evidence.head_sha,
        )
        if receipt_path.exists():
            _validate_review(existing, evidence, service)
            receipt = _read_receipt(receipt_path)
            if not _receipt_matches(receipt, evidence, service, key):
                raise ReviewPublicationError("receipt_idempotency_conflict")
            _record_cardstore_receipt(cards, evidence, receipt, receipt_path)
            return receipt

        if existing is None:
            connector.create_review(
                evidence.repository,
                evidence.number,
                commit_sha=evidence.head_sha,
                event="APPROVE",
                service_identity=service,
                idempotency_key=key,
            )
        _validate_live(
            evidence,
            service=service,
            connector=connector,
            cards=cards,
            protections=protections,
            approved_evidence_root=approved_evidence_root,
        )
        approval = _validate_review(
            connector.find_approval(
                evidence.repository,
                evidence.number,
                service_identity=service,
                head_sha=evidence.head_sha,
            ),
            evidence,
            service,
        )
        moment = clock()
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ReviewPublicationError("publication_clock_must_be_timezone_aware")
        payload = {
            "schema": "skfleet.seraph-review-publication/v1",
            "idempotency_key": key,
            "repository": evidence.repository,
            "number": evidence.number,
            "head_sha": evidence.head_sha,
            "source_card": evidence.source_card,
            "source_card_revision": evidence.source_card_revision,
            "review_card": evidence.review_card,
            "review_card_revision": evidence.review_card_revision,
            "evidence_sha256": evidence.evidence_sha256,
            "reviewer_identity": evidence.reviewer_identity.lower(),
            "service_identity": service,
            "github_review_id": approval.review_id,
            "published_at": moment.astimezone(timezone.utc).isoformat(),
        }
        receipt = PublicationReceipt(**payload, receipt_sha256=_digest(payload))
        receipt.validate()
        _write_receipt(receipt_path, receipt)
        _record_cardstore_receipt(cards, evidence, receipt, receipt_path)
        return receipt


__all__ = [
    "AuthenticatedCaller",
    "BranchProtection",
    "CapAuthVerifier",
    "CardSnapshot",
    "ConnectorCapabilities",
    "LiveCardStoreGateway",
    "LivePullRequest",
    "PublicationReceipt",
    "PublishedReview",
    "ReviewPublicationError",
    "SERAPH_CAPAUTH_URI",
    "SeraphPassEvidence",
    "VerifiedCaller",
    "_digest",
    "publish_seraph_pass",
]
