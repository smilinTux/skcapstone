"""Validate bounded review work carried by an incomplete Link lineage manifest."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from skcoord.card_store import CardCore, CardStore

from .seat_boundaries import BoundaryError
from .seat_runtime import authorize_review_launch, recommend_reviewer

_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CARD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_RECOMMENDATIONS = 50


@dataclass(frozen=True)
class ReviewWorkResult:
    """One deterministic review-card reconciliation result."""

    source_card: str
    head_revision: str
    review_card_id: str
    created: bool
    launchable: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def review_card_id(source_card: str, head_revision: str) -> str:
    """Return the stable cross-host identity for one source generation."""

    return hashlib.sha256(f"{source_card}\0{head_revision}".encode()).hexdigest()[:8]


def reconcile_review_work(
    home: Path,
    item: dict[str, Any],
    *,
    evidence_sha256: str,
    _cards_by_key: dict[tuple[str, str], list[Any]] | None = None,
) -> ReviewWorkResult:
    """Materialize once and prove the exact card passes reviewer preflight."""

    source_card = str(item["source_card"])
    head_revision = str(item["head_revision"])
    card_id = review_card_id(source_card, head_revision)
    store = CardStore(home)
    if _cards_by_key is None:
        _cards_by_key = _review_card_index(store.list_cards())
    key = (source_card, head_revision)
    matching = _cards_by_key.get(key, [])
    if len({card.id for card in matching}) > 1:
        return ReviewWorkResult(
            source_card, head_revision, card_id, False, False, "duplicate_review_cards"
        )
    if matching and matching[0].id != card_id:
        return ReviewWorkResult(
            source_card, head_revision, card_id, False, False, "noncanonical_review_card"
        )
    source = store.fold(source_card)
    if source is None:
        return ReviewWorkResult(
            source_card, head_revision, card_id, False, False, "source_card_missing"
        )
    existing = matching[0] if matching else store.fold(card_id)
    if (
        existing is not None
        and not matching
        and (
            existing.meta.get("link_source_card") != source_card
            or existing.meta.get("link_head_revision") != head_revision
        )
    ):
        return ReviewWorkResult(
            source_card, head_revision, card_id, False, False, "review_card_id_collision"
        )
    created = existing is None
    reviewer = dict(item["reviewer_candidates"][0])
    reviewer_identity = str(reviewer["identity"])
    source_owner = str(item["source_owner"])
    if created:
        try:
            store.create(
                CardCore(
                    id=card_id,
                    title=(
                        f"[LINK-{source_card}-{head_revision[:8]}][S][REVIEW] "
                        "Review exact source head"
                    ),
                    description=(
                        f"Producer identity: {source_owner}. "
                        f"Candidate evidence sha256={evidence_sha256}."
                    ),
                    created_by="link",
                    created_at=source.created_at,
                    acceptance_criteria=[
                        f"Review source card {source_card} at exact head {head_revision}.",
                        "Return a terminal PASS, FAIL, or BLOCKED verdict with evidence.",
                    ],
                    initial_labels=["review", "seat-seraph", f"parent-{source_card}"],
                    meta={
                        "link_source_card": source_card,
                        "link_head_revision": head_revision,
                    },
                )
            )
        except ValueError as exc:
            return ReviewWorkResult(source_card, head_revision, card_id, False, False, str(exc))
        created_card = store.fold(card_id)
        if created_card is not None:
            _cards_by_key[key] = [created_card]
        store.append_event(
            card_id,
            "link",
            "link",
            transition_id=f"link-review-producer-{card_id}",
            link_key="producer_identity",
            link_value=source_owner,
        )
        store.append_event(
            card_id,
            "link",
            "link",
            transition_id=f"link-review-evidence-{card_id}",
            link_key="candidate_evidence_sha256",
            link_value=evidence_sha256,
        )
    card = store.fold(card_id)
    parent_labels = [label for label in card.labels if label.startswith("parent-")] if card else []
    if card is None or parent_labels != [f"parent-{source_card}"] or "review" not in card.labels:
        return ReviewWorkResult(
            source_card, head_revision, card_id, created, False, "review_parent_invalid"
        )
    recommendation_id = f"link-review-{card_id}"
    try:
        recommendation = recommend_reviewer(
            home,
            card_id=card_id,
            recommendation_id=recommendation_id,
            author=source_owner,
            candidates=[reviewer_identity],
            observed_process={"sessions": []},
            evidence_sha256=evidence_sha256,
        )
        authorize_review_launch(
            home,
            recommendation,
            actor=reviewer_identity,
            current_process={"sessions": []},
            used_recommendation_ids=set(),
        )
    except BoundaryError as exc:
        return ReviewWorkResult(source_card, head_revision, card_id, created, False, str(exc))
    return ReviewWorkResult(source_card, head_revision, card_id, created, True, "ready")


def _review_card_index(cards: Iterable[Any]) -> dict[tuple[str, str], list[Any]]:
    """Index existing Link review cards in one CardStore scan."""

    result: dict[tuple[str, str], list[Any]] = {}
    for card in cards:
        source = str(card.meta.get("link_source_card") or "")
        head = str(card.meta.get("link_head_revision") or "")
        if source and head:
            result.setdefault((source, head), []).append(card)
    return result


def reconcile_review_work_batch(
    home: Path,
    recommendations: Iterable[dict[str, Any]],
    *,
    evidence_sha256: str,
) -> list[ReviewWorkResult]:
    """Reconcile one bounded batch with a single existing-card scan."""

    index = _review_card_index(CardStore(home).list_cards())
    return [
        reconcile_review_work(
            home,
            item,
            evidence_sha256=evidence_sha256,
            _cards_by_key=index,
        )
        for item in recommendations
    ]


def load_review_work(path: Path) -> tuple[str, str, list[dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != "skfleet.link-lineage/v1":
        raise ValueError("lineage schema invalid")
    evidence = str(data.get("evidence_hash") or "")
    unsigned = dict(data)
    unsigned.pop("evidence_hash", None)
    encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if not _DIGEST.fullmatch(evidence) or hashlib.sha256(encoded.encode()).hexdigest() != evidence:
        raise ValueError("lineage evidence invalid")
    source_revision = str(data.get("source_revision") or "")
    if not _DIGEST.fullmatch(source_revision):
        raise ValueError("lineage source revision invalid")
    raw = data.get("review_work_recommendations")
    if not isinstance(raw, list) or len(raw) > MAX_RECOMMENDATIONS:
        raise ValueError("review work bound invalid")
    recommendations: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("kind") != "review-work":
            raise ValueError("review work malformed")
        if item.get("reason") not in {"missing_terminal_review", "review_not_bound_to_head"}:
            raise ValueError("review work reason invalid")
        if not _SHA.fullmatch(str(item.get("head_revision") or "")):
            raise ValueError("review work head invalid")
        if not _SHA.fullmatch(str(item.get("base_revision") or "")):
            raise ValueError("review work base invalid")
        if not _DIGEST.fullmatch(str(item.get("card_generation") or "")):
            raise ValueError("review work card generation invalid")
        if not _CARD_ID.fullmatch(str(item.get("source_card") or "")):
            raise ValueError("review work source card invalid")
        reviewers = item.get("reviewer_candidates")
        owner = str(item.get("source_owner") or "")
        if not owner.strip():
            raise ValueError("review work source owner invalid")
        if not isinstance(reviewers, list) or not reviewers:
            raise ValueError("review work reviewer missing")
        if any(
            not isinstance(reviewer, dict)
            or reviewer.get("seat") != "seraph"
            or reviewer.get("eligible") is not True
            or reviewer.get("identity") == owner
            or reviewer.get("name") == owner
            for reviewer in reviewers
        ):
            raise ValueError("review work reviewer not distinct")
        recommendations.append(dict(item))
    return source_revision, evidence, recommendations
