"""Read-only diagnostics for the installed governed-review admission contract."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .card_store import CardStore
from .review_admission import (
    dependency_blocker_unresolved,
    governed_review_gate_reasons,
    governed_review_metadata,
    governed_review_seat,
    qualified_reviewer_seats,
    reviewer_capacity_evaluation,
)


def _awaiting_acceptance(home: Path, store: CardStore, card) -> bool:
    """Read pending PASS without launching the rotator or clearing any hold."""
    from .fleet.review_retire import retired_offers
    from .seraph_review_cardstore import _latest_outcome

    if "review" not in card.labels or card.archived or card.status.value == "done":
        return False
    outcome = _latest_outcome(store, card.id)
    value = (
        outcome.get("verdict")
        if outcome.get("action") == "verdict"
        else outcome.get("link_value", outcome.get("value"))
    )
    if not re.match(r"^\s*PASS(?:_FOR_[A-Z_]+)?\b", str(value or ""), re.I):
        return False
    rows = store._read_events(card.id) + store._legacy_events(card.id)
    try:
        boundary = datetime.fromisoformat(outcome["ts"].replace("Z", "+00:00"))
        later = [
            e for e in rows if datetime.fromisoformat(e["ts"].replace("Z", "+00:00")) > boundary
        ]
    except (KeyError, TypeError, ValueError):
        return True
    if any(
        e.get("action") == "move" and str(e.get("column") or "").strip().lower() == "ready"
        for e in later
    ):
        return False
    supersessions = [e for e in later if e.get("action") == "remote_review_supersede"]
    if supersessions:
        retired = retired_offers(home, card.id, rows)
        if any(e.get("request_id") in retired for e in supersessions):
            return False
    return True


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
    capacity = reviewer_capacity_evaluation(
        home, core, card.labels, metadata[0] if metadata else "", "diagnostic-reviewer"
    )
    busy = sum(capacity["occupancy"].values())
    target = int(capacity["logical_available"])
    labels = {str(label).strip().lower() for label in card.labels}
    reasons = list(
        governed_review_gate_reasons(
            core,
            card.labels,
            dependency_blocked=dependency_blocked,
            owned=card.owner is not None,
            capacity_available=capacity["reason"] == "eligible",
            dependency_blocker_holds=dependency_blocker_unresolved(home, core, card.labels),
            awaiting_acceptance=_awaiting_acceptance(home, store, card),
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
        "capacity": {
            "busy": busy,
            "target": target,
            "available": int(capacity["available"]),
            "reason": capacity["reason"],
            "revision": capacity["capacity_revision"],
        },
    }
