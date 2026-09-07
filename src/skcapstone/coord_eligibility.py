"""Leaf eligibility counts over the authoritative CardStore fold."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Collection

from .card import Column, Kind
from .card_store import CardStore

_EXCLUDED_LABELS = frozenset({"do-not-claim", "not-claimable", "superseded"})
_CONTAINER_LABELS = frozenset({"parent-container", "sprint-container"})
_FOREIGN_LABELS = frozenset({"foreign-project"})


@dataclass(frozen=True)
class LeafEligibilityCounts:
    """Separate actionable, review, and malformed CardStore populations."""

    leaves: int = 0
    review: int = 0
    malformed: int = 0


def _has_excluded_label(labels: set[str]) -> bool:
    """Return whether labels explicitly prohibit a claim."""
    return bool(labels & _EXCLUDED_LABELS) or any(
        label.startswith("superseded-") or "do-not-claim" in label for label in labels
    )


def _is_container(card, labels: set[str], parent_ids: set[str]) -> bool:
    """Return whether a folded card is a parent, epic, or sprint container."""
    title = card.title.lower()
    return bool(
        card.kind == Kind.EPIC
        or card.id in parent_ids
        or labels & _CONTAINER_LABELS
        or "[epic]" in title
        or "[sprint " in title
    )


def dispatch_policy_facts(card, by_id: dict[str, object], parent_ids: set[str]) -> dict[str, bool]:
    """Return the canonical claimability predicates for one folded card.

    This is the single dispatch policy surface. Diagnostics and selectors must
    consume these predicates instead of re-deriving eligibility independently.
    """
    labels = {str(label).lower() for label in card.labels}
    missing_dependency = any(
        dependency not in by_id or by_id[dependency].status != Column.DONE
        for dependency in card.dependencies
    )
    return {
        "container": _is_container(card, labels, parent_ids),
        "excluded": _has_excluded_label(labels),
        "foreign_project": bool(labels & _FOREIGN_LABELS),
        "human_gate": "human-gate" in labels or "[human]" in card.title.lower(),
        "missing_dependency": missing_dependency,
        "task": card.kind == Kind.TASK,
        "malformed": not card.id.strip() or card.title.strip().lower() in {"", "x"},
    }


def leaf_eligibility_counts(
    home: Path, selected_ids: Collection[str] | None = None
) -> LeafEligibilityCounts:
    """Count unowned dependency-complete leaves from the current card fold.

    Args:
        home: Shared SKCapstone root containing ``cards/``.
        selected_ids: Optional card IDs retained by status filters.

    Returns:
        Separate counts for backlog leaves, review work requiring a reviewer,
        and malformed candidate records. Missing dependencies fail closed.
    """
    cards = CardStore(home).list_cards()
    by_id = {card.id: card for card in cards}
    parent_ids = {
        label.removeprefix("parent-")
        for card in cards
        for label in card.labels
        if label.lower().startswith("parent-") and len(label) > len("parent-")
    }
    selected = set(selected_ids) if selected_ids is not None else None
    leaves = review = malformed = 0

    for card in cards:
        labels = {label.lower() for label in card.labels}
        if selected is not None and card.id not in selected:
            continue
        facts = dispatch_policy_facts(card, by_id, parent_ids)
        if not facts["task"]:
            continue
        if card.status not in {Column.BACKLOG, Column.REVIEW} or card.owner:
            continue
        if (
            facts["container"]
            or facts["excluded"]
            or facts["foreign_project"]
            or facts["human_gate"]
            or facts["missing_dependency"]
        ):
            continue
        if facts["malformed"]:
            malformed += 1
        elif card.status == Column.REVIEW:
            review += 1
        else:
            leaves += 1

    return LeafEligibilityCounts(leaves=leaves, review=review, malformed=malformed)
