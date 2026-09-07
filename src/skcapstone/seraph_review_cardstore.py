"""Live CardStore adapter for Seraph review publication."""

from __future__ import annotations

import os
from pathlib import Path

from .card_store import CardStore
from .seraph_review_contracts import (
    CardSnapshot,
    ReviewPublicationError,
    _digest,
    _text,
)

RECEIPT_LINK = "seraph-review-publication-receipt"
EVIDENCE_LINK = "evidence"
EVIDENCE_HASH_LINK = "evidence_sha256"


def card_revision(card: object) -> str:
    """Hash stable card facts, excluding this publisher's own receipt link."""

    payload = card.model_dump(mode="json")  # type: ignore[attr-defined]
    payload.pop("updated_at", None)
    links = dict(payload.get("links") or {})
    links.pop(RECEIPT_LINK, None)
    payload["links"] = links
    return _digest(payload)


class LiveCardStoreGateway:
    """Use the local event-sourced CardStore for current reads and receipts."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.store = CardStore(home)

    def read_card(self, card_id: str) -> CardSnapshot:
        try:
            card = self.store.fold(card_id)
        except (OSError, TypeError, ValueError) as exc:
            raise ReviewPublicationError("cardstore_read_failed") from exc
        if card is None or card.id != card_id:
            raise ReviewPublicationError("card_not_found")
        parents = {
            label.removeprefix("parent-")
            for label in card.labels
            if isinstance(label, str) and label.startswith("parent-")
        }
        if len(parents) > 1:
            raise ReviewPublicationError("card_parent_ambiguous")
        verdict = card.links.get("verdict")
        evidence_link = card.links.get(EVIDENCE_LINK)
        evidence_hash = card.links.get(EVIDENCE_HASH_LINK)
        evidence_path: str | None = None
        if isinstance(evidence_link, str):
            evidence_path, separator, linked_hash = evidence_link.partition("#sha256=")
            if not separator or linked_hash != evidence_hash:
                raise ReviewPublicationError("card_evidence_link_mismatch")
        return CardSnapshot(
            card_id=card.id,
            revision=card_revision(card),
            status=getattr(card.status, "value", str(card.status)),
            verdict=verdict if isinstance(verdict, str) else None,
            parent_id=next(iter(parents), None),
            evidence_path=evidence_path,
            evidence_sha256=evidence_hash if isinstance(evidence_hash, str) else None,
        )

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
        link_value = f"{receipt_path}#sha256={receipt_sha256}"
        try:
            event = self.store.append_event(
                review_card,
                "link",
                "seraph",
                transition_id=transition_id,
                link_key=RECEIPT_LINK,
                link_value=link_value,
                schema="skfleet.seraph-review-publication-link/v1",
                receipt_sha256=receipt_sha256,
                github_review_id=github_review_id,
                head_sha=head_sha,
            )
            expected = {
                "action": "link",
                "writer": "seraph",
                "transition_id": transition_id,
                "link_key": RECEIPT_LINK,
                "link_value": link_value,
                "schema": "skfleet.seraph-review-publication-link/v1",
                "receipt_sha256": receipt_sha256,
                "github_review_id": github_review_id,
                "head_sha": head_sha,
            }
            if any(event.get(key) != value for key, value in expected.items()):
                raise ReviewPublicationError("cardstore_receipt_event_mismatch")
            if not self.store.has_transition(review_card, transition_id):
                raise ReviewPublicationError("cardstore_receipt_not_durable")
            descriptor = os.open(
                self.home / "cards" / review_card / "events",
                os.O_RDONLY | os.O_DIRECTORY,
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except ReviewPublicationError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise ReviewPublicationError("cardstore_receipt_write_failed") from exc
        return _text(event.get("event_id"), "cardstore_event_id")
