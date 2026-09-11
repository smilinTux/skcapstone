"""Read-only diagnostics for the installed governed-review admission contract."""

from __future__ import annotations

import os
from pathlib import Path

from .card_store import CardStore
from .review_admission import governed_review_gate_reasons


def seraph_capacity_target() -> int:
    """Return the same effective target used by the Seraph seat cycle."""
    raw = os.environ.get("SKFLEET_SEAT_TARGET")
    if raw is None:
        raw = os.environ.get("SKFLEET_SERAPH_BATCH_SIZE", "2")
    return max(0, int(raw))


def diagnose(home: Path, card_id: str) -> dict[str, object]:
    """Explain one card using the same review gates consumed by POOL_V2."""
    store = CardStore(Path(home).expanduser())
    card = store.fold(card_id)
    if card is None:
        return {"card_id": card_id, "eligible": False, "reasons": ["unknown-card"]}

    # Every gate decision is derived from one fresh authoritative fold.  Do not
    # use a kanban projection or cached eligibility result: labels, dependencies
    # and void events are all admission facts.
    cards = {row.id: row for row in store.list_cards(include_archived=True)}
    labels = {str(label).strip().lower() for label in card.labels}
    structural_reasons: list[str] = []
    if card.meta.get("voided"):
        structural_reasons.append("voided-card")
    if labels & {"do-not-claim", "not-claimable", "human-gate", "superseded"} or any(
        label.startswith("superseded-") or "do-not-claim" in label for label in labels
    ):
        if "human-gate" in labels:
            structural_reasons.append("human-gate")
        else:
            structural_reasons.append("do-not-claim")
    dependency_blocked = any(
        dependency not in cards
        or cards[dependency].status.value != "done"
        or cards[dependency].meta.get("voided")
        for dependency in card.dependencies
    )
    target = seraph_capacity_target()
    busy = sum(
        row.id != card.id
        and row.owner is not None
        and row.status.value in {"ready", "doing", "review"}
        and "seat-seraph" in {str(label).strip().lower() for label in row.labels}
        for row in cards.values()
    )
    reasons = list(
        governed_review_gate_reasons(
            {
                "id": card.id,
                "title": card.title,
                "description": card.description,
                "links": card.links,
                "meta": card.meta,
            },
            card.labels,
            dependency_blocked=dependency_blocked,
            owned=card.owner is not None,
            capacity_available=busy < target,
        )
    )
    reasons.extend(structural_reasons)
    if dependency_blocked and "dependency" not in reasons:
        reasons.append("dependency")
    if card.status.value in {"done", "archived", "void"}:
        reasons.append("terminal")
    return {
        "card_id": card.id,
        "eligible": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "seat": (
            "seraph"
            if "seat-seraph" in {str(label).strip().lower() for label in card.labels}
            else None
        ),
        "capacity": {"busy": busy, "target": target},
    }
