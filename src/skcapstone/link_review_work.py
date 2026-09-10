"""Validate bounded review work carried by an incomplete Link lineage manifest."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from skcoord.card_store import CardCore, CardStore, card_mutation_lock

from .seat_boundaries import BoundaryError
from .seat_runtime import authorize_review_launch, recommend_reviewer

_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CARD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_RECOMMENDATIONS = 50
_BASE_REF = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")


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


def card_generation(card: Any) -> str:
    """Return the canonical Link generation for one folded source card."""

    def value(name: str, default: Any = None) -> Any:
        return card.get(name, default) if isinstance(card, dict) else getattr(card, name, default)

    links = value("links", {}) or {}
    status = value("status")
    stable = {
        "id": value("id"),
        "status": getattr(status, "value", status),
        "owner": value("owner"),
        "labels": sorted(value("labels", []) or []),
        "dependencies": sorted(value("dependencies", []) or []),
        "verdict": str(links.get("verdict") or links.get("outcome") or "").strip().upper(),
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def review_card_id(
    source_card: str,
    head_revision: str,
    card_generation: str,
    evidence_sha256: str,
    review_class: str = "review",
) -> str:
    """Return the stable cross-host identity for one source generation."""

    identity = "\0".join(
        (source_card, head_revision, card_generation, evidence_sha256, review_class)
    )
    return hashlib.sha256(identity.encode()).hexdigest()[:8]


def _validated_workspace_binding(repository: object, base_ref: object) -> tuple[str, str]:
    """Validate one credential-free source workspace binding."""

    repository_value = str(repository or "").strip()
    base_ref_value = str(base_ref or "").strip()
    parsed = urlsplit(repository_value)
    if (
        len(repository_value) > 2048
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("source_workspace_repository_invalid")
    if not _BASE_REF.fullmatch(base_ref_value) or base_ref_value.startswith("-"):
        raise ValueError("source_workspace_base_ref_invalid")
    return repository_value, base_ref_value


def _source_workspace_binding(source: Any, item: dict[str, Any]) -> tuple[str, str]:
    """Resolve one exact recommendation or folded-parent workspace binding."""

    links = source.links if hasattr(source, "links") else source.get("links", {})
    parent_raw = (links.get("repository"), links.get("base_ref"))
    recommendation_present = "workspace_repository" in item or "base_ref" in item
    parent_present = bool(parent_raw[0] or parent_raw[1])
    recommendation = None
    parent = None
    if recommendation_present:
        recommendation = _validated_workspace_binding(
            item.get("workspace_repository"), item.get("base_ref")
        )
    if parent_present:
        parent = _validated_workspace_binding(*parent_raw)
    if recommendation is not None and parent is not None and recommendation != parent:
        raise ValueError("source_workspace_binding_conflict")
    binding = recommendation or parent
    if binding is None:
        raise ValueError("source_workspace_binding_missing")
    return binding


def _persist_workspace_binding(
    store: CardStore, card_id: str, repository: str, base_ref: str, base_revision: str
) -> None:
    """Idempotently inherit an exact parent workspace onto a canonical review card."""

    card = store.fold(card_id)
    if card is None:
        raise ValueError("review_card_missing")
    for key, expected in (
        ("repository", repository),
        ("base_ref", base_ref),
        ("base_revision", base_revision),
    ):
        observed = str(card.links.get(key) or "").strip()
        if observed and observed != expected:
            raise ValueError("review_workspace_binding_conflict")
        if not observed:
            store.append_event(
                card_id,
                "link",
                "link",
                transition_id=f"link-review-workspace-{key}-{card_id}",
                link_key=key,
                link_value=expected,
            )
    if "source-only" not in card.labels:
        store.append_event(
            card_id,
            "add_label",
            "link",
            transition_id=f"link-review-workspace-label-{card_id}",
            label="source-only",
        )


def reconcile_review_work(
    home: Path,
    item: dict[str, Any],
    *,
    evidence_sha256: str,
    _cards_by_key: dict[tuple[str, str, str, str, str], list[Any]] | None = None,
) -> ReviewWorkResult:
    """Materialize once and prove the exact card passes reviewer preflight."""

    source_card = str(item["source_card"])
    head_revision = str(item["head_revision"])
    base_revision = str(item.get("base_revision") or "").lower()
    supplied_generation = str(item["card_generation"])
    review_class = "review"
    card_id = review_card_id(
        source_card,
        head_revision,
        supplied_generation,
        evidence_sha256,
        review_class,
    )
    if not _DIGEST.fullmatch(supplied_generation):
        return ReviewWorkResult(
            source_card, head_revision, card_id, False, False, "source_generation_invalid"
        )
    if not _DIGEST.fullmatch(evidence_sha256):
        return ReviewWorkResult(
            source_card, head_revision, card_id, False, False, "candidate_evidence_invalid"
        )
    if not _SHA.fullmatch(base_revision):
        return ReviewWorkResult(
            source_card, head_revision, card_id, False, False, "review_work_base_invalid"
        )
    store = CardStore(home)
    try:
        source_lock = card_mutation_lock(home, source_card, artifact_neutral=True)
        source_lock.__enter__()
    except ValueError:
        return ReviewWorkResult(
            source_card, head_revision, card_id, False, False, "source_card_missing"
        )
    try:
        try:
            source = store.fold(source_card)
        except ValueError:
            return ReviewWorkResult(
                source_card, head_revision, card_id, False, False, "source_card_malformed"
            )
        if source is None:
            return ReviewWorkResult(
                source_card, head_revision, card_id, False, False, "source_card_missing"
            )
        if card_generation(source) != supplied_generation:
            return ReviewWorkResult(
                source_card, head_revision, card_id, False, False, "source_generation_changed"
            )
        try:
            repository, base_ref = _source_workspace_binding(source, item)
        except ValueError as exc:
            return ReviewWorkResult(source_card, head_revision, card_id, False, False, str(exc))
        if _cards_by_key is None:
            _cards_by_key = _review_card_index(store.list_cards())
        key = (source_card, head_revision, supplied_generation, evidence_sha256, review_class)
        matching = _cards_by_key.get(key, [])
        if len({card.id for card in matching}) > 1:
            return ReviewWorkResult(
                source_card, head_revision, card_id, False, False, "duplicate_review_cards"
            )
        if matching and matching[0].id != card_id:
            return ReviewWorkResult(
                source_card, head_revision, card_id, False, False, "noncanonical_review_card"
            )
        existing = matching[0] if matching else store.fold(card_id)
        expected_meta = {
            "link_source_card": source_card,
            "link_head_revision": head_revision,
            "link_card_generation": supplied_generation,
            "link_evidence_sha256": evidence_sha256,
            "link_review_class": review_class,
            "base_revision": base_revision,
        }
        if (
            existing is not None
            and not matching
            and any(existing.meta.get(name) != value for name, value in expected_meta.items())
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
                        initial_labels=[
                            "review",
                            "seat-seraph",
                            "source-only",
                            f"parent-{source_card}",
                        ],
                        meta=expected_meta,
                    )
                )
            except ValueError as exc:
                return ReviewWorkResult(
                    source_card, head_revision, card_id, False, False, str(exc)
                )
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
        try:
            _persist_workspace_binding(store, card_id, repository, base_ref, base_revision)
        except ValueError as exc:
            return ReviewWorkResult(source_card, head_revision, card_id, created, False, str(exc))
        card = store.fold(card_id)
        parent_labels = (
            [label for label in card.labels if label.startswith("parent-")] if card else []
        )
        if (
            card is None
            or parent_labels != [f"parent-{source_card}"]
            or "review" not in card.labels
        ):
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
            try:
                current_binding = _source_workspace_binding(store.fold(source_card), item)
            except (AttributeError, ValueError):
                current_binding = None
            if current_binding != (repository, base_ref):
                return ReviewWorkResult(
                    source_card,
                    head_revision,
                    card_id,
                    created,
                    False,
                    "source_workspace_binding_drifted",
                )
            if card_generation(store.fold(source_card)) != supplied_generation:
                return ReviewWorkResult(
                    source_card,
                    head_revision,
                    card_id,
                    created,
                    False,
                    "source_generation_changed",
                )
            authorize_review_launch(
                home,
                recommendation,
                actor=reviewer_identity,
                current_process={"sessions": []},
                used_recommendation_ids=set(),
            )
        except (BoundaryError, ValueError) as exc:
            return ReviewWorkResult(source_card, head_revision, card_id, created, False, str(exc))
        return ReviewWorkResult(source_card, head_revision, card_id, created, True, "ready")
    finally:
        source_lock.__exit__(None, None, None)


def _review_card_index(cards: Iterable[Any]) -> dict[tuple[str, str, str, str, str], list[Any]]:
    """Index existing Link review cards in one CardStore scan."""

    result: dict[tuple[str, str, str, str, str], list[Any]] = {}
    for card in cards:
        source = str(card.meta.get("link_source_card") or "")
        head = str(card.meta.get("link_head_revision") or "")
        generation = str(card.meta.get("link_card_generation") or "")
        evidence = str(card.meta.get("link_evidence_sha256") or "")
        review_class = str(card.meta.get("link_review_class") or "")
        if source and head and generation and evidence and review_class:
            result.setdefault((source, head, generation, evidence, review_class), []).append(card)
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
