"""Publish current private-forge reviews from the mediated Link observation feed."""

from __future__ import annotations

import os
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .fleet.operator_http import canonical_request_bytes
from .forgejo import SKGIT_ORIGIN, SKGIT_REPOSITORY, ForgejoClient
from .link_observation_feed import LinkObservationFeed
from .seraph_forgejo import (
    ForgejoReviewConnector,
    ProvisionedCredential,
)
from .seraph_review_capauth import (
    PUBLISH_METHOD,
    PUBLISH_PATH,
    SERAPH_FINGERPRINT,
    SERAPH_PRINCIPAL,
    DurableNonceCache,
    JsonlAuditSink,
    SeraphCapAuthVerifier,
)
from .seraph_review_cardstore import LiveCardStoreGateway
from .seraph_review_contracts import (
    AuthenticatedCaller,
    ReviewPublicationError,
    SeraphPassEvidence,
)
from .seraph_review_credentials import IdentityReadClient, attest_credentials, read_credentials
from .seraph_review_publisher import publication_request_sha256, publish_seraph_pass

SERVICE_IDENTITY = "seraph-review-bot"


def _invalid_credential_value(value: str) -> bool:
    return not value or any(character.isspace() for character in value)


def read_credential(path: Path) -> ProvisionedCredential:
    """Read one owner-only regular token file without accepting shell syntax."""
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ReviewPublicationError("forge_credential_file_invalid")
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise ReviewPublicationError("forge_credential_file_permissions_invalid")
    lines = path.read_text(encoding="utf-8").splitlines()
    values = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or key in values:
            raise ReviewPublicationError("forge_credential_file_invalid")
        values[key] = value
    expected = {
        "SKGIT_TOKEN",
        "SKGIT_TOKEN_SHA256",
        "SKGIT_TOKEN_ID",
        "SKGIT_TOKEN_NAME",
        "SKGIT_TOKEN_SCOPES",
        "SKGIT_PROVISIONED_REPOSITORY",
    }
    invalid_value = any(map(_invalid_credential_value, values.values()))
    if set(values) != expected or invalid_value:
        raise ReviewPublicationError("forge_credential_file_invalid")
    try:
        token_id = int(values["SKGIT_TOKEN_ID"])
    except ValueError as exc:
        raise ReviewPublicationError("forge_credential_file_invalid") from exc
    return ProvisionedCredential(
        token=values["SKGIT_TOKEN"],
        token_sha256=values["SKGIT_TOKEN_SHA256"],
        token_id=token_id,
        token_name=values["SKGIT_TOKEN_NAME"],
        scopes=tuple(values["SKGIT_TOKEN_SCOPES"].split(",")),
        repositories=tuple(values["SKGIT_PROVISIONED_REPOSITORY"].split(",")),
    )


def _evidence(record, cards: LiveCardStoreGateway) -> SeraphPassEvidence:
    observation = record.observation
    if (
        observation.repository != SKGIT_REPOSITORY
        or observation.ci_state != "success"
        or record.review_card_id is None
        or observation.source_card is None
        or tuple(value.upper() for value in observation.lineage_outcomes) != ("PASS",)
    ):
        raise ReviewPublicationError("observation_not_publishable")
    source = cards.read_card(observation.source_card)
    review = cards.read_card(record.review_card_id)
    if not review.evidence_sha256:
        raise ReviewPublicationError("card_evidence_missing")
    return SeraphPassEvidence(
        repository=observation.repository,
        number=observation.number,
        head_sha=observation.head_sha,
        source_card=source.card_id,
        source_card_revision=source.revision,
        review_card=review.card_id,
        review_card_revision=review.revision,
        reviewer_identity=SERAPH_PRINCIPAL,
        verdict="PASS",
        evidence_sha256=review.evidence_sha256,
    )


def signed_caller(
    evidence: SeraphPassEvidence,
    signer: Callable[[bytes], str],
    *,
    now: datetime | None = None,
    nonce: str | None = None,
) -> AuthenticatedCaller:
    """Sign exactly the digest the publisher will authorize."""
    request_sha256 = publication_request_sha256(evidence, SERVICE_IDENTITY)
    timestamp = (now or datetime.now(timezone.utc)).timestamp()
    timestamp_text = str(timestamp)
    nonce_text = nonce or secrets.token_hex(24)
    body = request_sha256.encode("ascii")
    signature = signer(
        canonical_request_bytes(PUBLISH_METHOD, PUBLISH_PATH, body, timestamp_text, nonce_text)
    )
    return AuthenticatedCaller(
        {
            "headers": {
                "X-SK-Fingerprint": SERAPH_FINGERPRINT,
                "X-SK-Timestamp": timestamp_text,
                "X-SK-Nonce": nonce_text,
                "X-SK-Signature": signature,
            }
        }
    )


def publish_ready(
    feed: LinkObservationFeed,
    *,
    home: Path,
    evidence_root: Path,
    runtime_dir: Path,
    credential_file: Path,
    signer: Callable[[bytes], str],
) -> list[str]:
    """Publish each exact private PASS in a fresh mediated feed."""
    cards = LiveCardStoreGateway(home)
    credentials = read_credentials(credential_file)
    client = ForgejoClient(SKGIT_ORIGIN, credentials.writer.token)
    identity_client = IdentityReadClient(SKGIT_ORIGIN, credentials.identity.token)
    connector = ForgejoReviewConnector(
        client,
        attest_capabilities=lambda forge, repository: attest_credentials(
            identity_client, forge, credentials, repository
        ),
    )
    verifier = SeraphCapAuthVerifier(
        base_dir=home,
        nonce_store=DurableNonceCache(runtime_dir / "nonces.sqlite3"),
        audit_sink=JsonlAuditSink(runtime_dir / "authorization.jsonl"),
    )
    receipts = []
    for record in feed.records:
        if record.observation.repository != SKGIT_REPOSITORY:
            continue
        evidence = _evidence(record, cards)
        receipt = publish_seraph_pass(
            evidence,
            caller=signed_caller(evidence, signer),
            capauth=verifier,
            service_identity=SERVICE_IDENTITY,
            connector=connector,
            cards=cards,
            protections=connector,
            receipt_dir=runtime_dir / "receipts",
            approved_evidence_root=evidence_root,
        )
        receipts.append(receipt.receipt_sha256)
    return receipts
