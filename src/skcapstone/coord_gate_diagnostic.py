"""Read-only diagnostics for the installed governed-review admission contract."""

from __future__ import annotations

from pathlib import Path

from .card_store import CardStore
from .review_admission import (
    dependency_blocker_unresolved,
    governed_review_gate_reasons,
    governed_review_metadata,
    governed_review_seat,
    qualified_reviewer_seats,
    reviewer_capacity,
)


def diagnose(home: Path, card_id: str) -> dict[str, object]:
    """Explain one card using the same review gates consumed by POOL_V2."""
    store = CardStore(Path(home).expanduser())
    card = store.fold(card_id)
    if card is None:
        return {"card_id": card_id, "eligible": False, "reasons": ["unknown-card"]}

    cards = {row.id: row for row in store.list_cards()}
    dependency_blocked = any(
        dependency not in cards or cards[dependency].status.value != "done"
        for dependency in card.dependencies
    )
    core = {
        "id": card.id,
        "title": card.title,
        "description": card.description,
        "links": card.links,
        "meta": card.meta,
    }
    metadata = governed_review_metadata(core, card.labels)
    busy, target = reviewer_capacity(
        home, core, card.labels, metadata[0] if metadata else "", "diagnostic-reviewer"
    )
    labels = {str(label).strip().lower() for label in card.labels}
    reasons = list(
        governed_review_gate_reasons(
            core,
            card.labels,
            dependency_blocked=dependency_blocked,
            owned=card.owner is not None,
            capacity_available=busy < target,
            dependency_blocker_holds=dependency_blocker_unresolved(home, core, card.labels),
        )
    )
    if "do-not-claim" in labels:
        reasons.append("do-not-claim")
    if card.status.value == "done" or card.archived or card.meta.get("voided"):
        reasons.append("terminal")
    return {
        "card_id": card.id,
        "eligible": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "seat": governed_review_seat(card.labels, qualified_reviewer_seats(core)),
        "capacity": {"busy": busy, "target": target},
    }
