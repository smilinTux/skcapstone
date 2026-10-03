"""Current card bindings and mediated publication receipt writes."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from skcoord.card_store import CardStore

from .blocked_verdict import is_outcome_key
from .review_verdict import is_review_card
from .seraph_review_contracts import CardSnapshot, ReviewPublicationError, _digest

RECEIPT_LINK = "seraph-review-publication-receipt"


def card_revision(card: object) -> str:
    """Hash stable facts, excluding this publisher's own receipt link."""
    payload = card.model_dump(mode="json")
    payload.pop("updated_at", None)
    links = dict(payload.get("links") or {})
    links.pop(RECEIPT_LINK, None)
    payload["links"] = links
    return _digest(payload)


def _binding(card: object, *names: str) -> str | None:
    """Reject conflicting live links and creation metadata."""
    values = {
        str(mapping[name]).strip()
        for mapping in (card.links, card.meta)
        for name in names
        if mapping.get(name) is not None
    }
    if len(values) > 1:
        raise ReviewPublicationError("card_binding_conflict")
    return next(iter(values), None)


def _candidate(card: object) -> tuple[str | None, int | None, str | None]:
    """Resolve a forge-qualified repository, PR and exact candidate head."""
    repository = _binding(card, "repository")
    if repository:
        repository = repository.rstrip("/").removesuffix(".git")
        repository = repository.removeprefix("https://github.com/")
    pr = _binding(card, "pr")
    number = None
    if pr:
        parsed = urlsplit(pr)
        if parsed.scheme:
            match = re.fullmatch(r"/(.+)/pulls?/([1-9][0-9]*)", parsed.path)
            if (
                parsed.scheme != "https"
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
                or not match
            ):
                raise ReviewPublicationError("card_pr_binding_invalid")
            bound = f"{parsed.scheme}://{parsed.netloc}/{match[1]}"
            bound = bound.removeprefix("https://github.com/")
            if bound != repository:
                raise ReviewPublicationError("card_pr_repository_mismatch")
            number = int(match[2])
        elif pr.isdecimal() and int(pr) > 0:
            number = int(pr)
        else:
            raise ReviewPublicationError("card_pr_binding_invalid")
    head = _binding(card, "link_head_revision", "head", "head_commit", "commit")
    return repository, number, head


def _latest_outcome(store: CardStore, card_id: str) -> dict:
    """Read native and mediated outcome aliases with the same latest-event rule."""
    rows = store._read_events(card_id) + store._legacy_events(card_id)
    outcomes = [
        event
        for event in rows
        if event.get("action") == "verdict"
        or (
            event.get("action") == "link"
            and is_outcome_key(event.get("link_key") or event.get("key"))
        )
    ]
    return max(
        outcomes, key=lambda e: (e.get("ts", ""), e.get("writer", ""), e.get("seq", 0)), default={}
    )


class LiveCardStoreGateway:
    """Read authoritative folds; write only through skcapstone coord."""

    def __init__(self, home: Path) -> None:
        self.home = home

    def read_card(self, card_id: str) -> CardSnapshot:
        """Read current candidate and attributed verdict, never caller assertions."""
        store = CardStore(self.home)  # Refresh cached legacy projections on every check.
        try:
            card = store.fold(card_id)
            if card is None or card.id != card_id:
                raise ReviewPublicationError("card_not_found")
            events = store._read_events(card_id) + store._legacy_events(card_id)
            events.sort(key=lambda e: (e.get("ts", ""), e.get("writer", ""), e.get("seq", 0)))
        except (OSError, TypeError, ValueError) as exc:
            if isinstance(exc, ReviewPublicationError):
                raise
            raise ReviewPublicationError("cardstore_read_failed") from exc
        parents = {
            label.removeprefix("parent-")
            for label in card.labels
            if isinstance(label, str) and label.startswith("parent-")
        }
        if len(parents) > 1:
            raise ReviewPublicationError("card_parent_ambiguous")
        outcomes = [
            event
            for event in events
            if event.get("action") == "verdict"
            or (
                event.get("action") == "link"
                and is_outcome_key(event.get("link_key") or event.get("key"))
            )
        ]
        outcome = outcomes[-1] if outcomes else {}
        verdict = (
            outcome.get("verdict")
            if outcome.get("action") == "verdict"
            else (outcome.get("link_value", outcome.get("value")))
        )
        writer = outcome.get("writer")
        repository, number, head = _candidate(card)
        if outcome.get("action") == "verdict":
            candidate_head = outcome.get("candidate_commit")
            if head is not None and head != candidate_head:
                raise ReviewPublicationError("card_binding_conflict")
            head = candidate_head
            for event in events[events.index(outcome) + 1 :]:
                key = event.get("link_key") or event.get("key") or ""
                if key == "evidence_sha256" or key.startswith("blocked_on"):
                    raise ReviewPublicationError("source_generation_invalidated")
        is_review = is_review_card(card.title) or "review" in card.labels
        producer = _binding(card, "producer_identity") if is_review else writer
        candidate_hash = (
            _binding(card, "candidate_evidence_sha256")
            if is_review
            else outcome.get("candidate_sha256") or _binding(card, "candidate_evidence_sha256")
        )
        evidence_hash = _binding(card, "evidence_sha256", "reviewer_evidence_sha256")
        evidence_path = card.links.get("evidence")
        if isinstance(evidence_path, str) and "#sha256=" in evidence_path:
            evidence_path, linked_hash = evidence_path.rsplit("#sha256=", 1)
            if linked_hash != evidence_hash:
                raise ReviewPublicationError("card_evidence_link_mismatch")
        unresolved = False
        if is_review:
            for sibling in store.list_cards():
                if (
                    not (is_review_card(sibling.title) or "review" in sibling.labels)
                    or sibling.id == card_id
                    or not parents.intersection(
                        label.removeprefix("parent-")
                        for label in sibling.labels
                        if label.startswith("parent-")
                    )
                ):
                    continue
                if _candidate(sibling) != (repository, number, head):
                    continue
                sibling_outcome = _latest_outcome(store, sibling.id)
                sibling_verdict = sibling_outcome.get("verdict") or sibling_outcome.get(
                    "link_value", sibling_outcome.get("value")
                )
                if (
                    sibling_verdict != "PASS" or sibling.status.value != "done"
                ) and sibling.links.get("superseded_by") != card_id:
                    unresolved = True
        bindings = dict(
            repository=repository,
            number=number,
            head_sha=head,
            producer_identity=producer,
            reviewer_identity=writer if is_review else None,
            unresolved_review=unresolved,
            candidate_evidence_sha256=candidate_hash,
        )
        return CardSnapshot(
            card_id=card.id,
            revision=_digest(
                {"card": card_revision(card), "bindings": bindings, "outcome": outcome}
            ),
            status=getattr(card.status, "value", str(card.status)),
            verdict=verdict,
            parent_id=next(iter(parents), None),
            evidence_path=evidence_path,
            evidence_sha256=evidence_hash,
            **bindings,
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
        """Link one sealed receipt via the mediated CLI and verify its fold."""
        value = f"{receipt_path}#sha256={receipt_sha256}"
        try:
            card = CardStore(self.home).fold(review_card)
            existing = card.links.get(RECEIPT_LINK) if card else None
            if existing is not None and existing != value:
                raise ReviewPublicationError("cardstore_receipt_conflict")
            if existing is None:
                result = subprocess.run(
                    [
                        "skcapstone",
                        "coord",
                        "link",
                        review_card,
                        RECEIPT_LINK,
                        value,
                        "--home",
                        str(self.home),
                        "--agent",
                        "seraph",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if result.returncode != 0:
                    raise ReviewPublicationError("cardstore_receipt_write_failed")
            current = CardStore(self.home).fold(review_card)
            if current is None or current.links.get(RECEIPT_LINK) != value:
                raise ReviewPublicationError("cardstore_receipt_not_durable")
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReviewPublicationError("cardstore_receipt_write_failed") from exc
        return transition_id
