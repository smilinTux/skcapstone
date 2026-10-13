"""Read-only diagnostics for the installed governed-review admission contract."""

from __future__ import annotations

from pathlib import Path

from .card_store import CardStore
from .coord_amendments import is_voided
from .human_wait import label_gate
from .review_admission import (
    dependency_blocker_unresolved,
    governed_review_gate_reasons,
    governed_review_metadata,
    governed_review_seat,
    qualified_reviewer_seats,
    reviewer_capacity,
)


def structural_admission_reasons(home: Path, card, cards: dict[str, object]) -> tuple[str, ...]:
    """Return authoritative, non-bypassable holds for one folded card.

    CardStore has no void column: an authoritative void event therefore has to
    be consulted for both the candidate and each purportedly completed
    dependency.  Keep this small decision shared by diagnostics, selectors,
    and every local claim entrypoint rather than letting a projection decide.
    """
    labels = {str(label).strip().lower().replace("_", "-") for label in card.labels}
    # CardStore's FoldedCard always supplies ``dependencies``.  Defaulting an
    # omitted attribute to the immutable empty tuple keeps read-only callers
    # compatible with legacy folded-card adapters while real CardStore data
    # remains fail-closed for every declared dependency.
    dependencies = getattr(card, "dependencies", ())
    has_authoritative_dependencies = hasattr(card, "dependencies")
    dependency_blocked = any(
        dependency not in cards
        or cards[dependency].status.value != "done"
        or is_voided(home, dependency)
        for dependency in dependencies
    )
    reasons: list[str] = []
    if dependency_blocked:
        reasons.append("dependency")
    if "do-not-claim" in labels or any("do-not-claim" in label for label in labels):
        reasons.append("do-not-claim")
    human_gate = label_gate(card) if hasattr(card, "title") else "human-gate" in labels
    if human_gate:
        reasons.append("human-gate")
    if (
        card.status.value in {"done", "archived", "void"}
        or getattr(card, "archived", False)
        or getattr(card, "meta", {}).get("voided")
        # Non-CardStore compatibility adapters do not have an event log to
        # interrogate.  A real folded card always declares dependencies, so
        # only it may participate in the authoritative void-event check.
        or (has_authoritative_dependencies and is_voided(home, card.id))
    ):
        reasons.append("terminal")
    return tuple(reasons)


def diagnose(home: Path, card_id: str, *, card=None) -> dict[str, object]:
    """Explain one card using the same review gates consumed by POOL_V2.

    ``card`` may be supplied only by a caller that has just folded it during
    the same pre-claim admission sequence, avoiding a redundant read without
    using a cache or projection.
    """
    home = Path(home).expanduser()
    store = CardStore(home)
    if card is None:
        card = store.fold(card_id)
    if card is None:
        return {"card_id": card_id, "eligible": False, "reasons": ["unknown-card"]}

    cards = {row.id: row for row in store.list_cards()}
    structural_reasons = structural_admission_reasons(home, card, cards)
    dependency_blocked = "dependency" in structural_reasons
    core = {
        "id": card.id,
        "title": getattr(card, "title", ""),
        "description": getattr(card, "description", ""),
        "links": getattr(card, "links", {}),
        "meta": getattr(card, "meta", {}),
    }
    metadata = governed_review_metadata(core, card.labels)
    busy, target = reviewer_capacity(
        home, core, card.labels, metadata[0] if metadata else "", "diagnostic-reviewer"
    )
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
    reasons.extend(structural_reasons)
    return {
        "card_id": card.id,
        "eligible": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "seat": governed_review_seat(card.labels, qualified_reviewer_seats(core)),
        "capacity": {"busy": busy, "target": target},
    }


def assert_claim_admitted(home: Path, card_id: str, *, card=None) -> None:
    """Reject named claims using the same fresh fold as ``coord gates``.

    Board mutations deliberately do not perform policy reads.  Keeping this
    assertion beside the readback prevents a projection or an older selector
    from authorizing a card which the operator-facing gate reports held.
    """
    result = diagnose(home, card_id, card=card)
    blocked = {
        "unknown-card",
        "dependency",
        "do-not-claim",
        "human-gate",
        "voided",
        "terminal",
    }
    reasons = [reason for reason in result["reasons"] if reason in blocked]
    if reasons:
        raise ValueError("coord claim denied: " + ", ".join(reasons))
