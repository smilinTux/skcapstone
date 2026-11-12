"""Read-only card decomposition preflight: a recommendation, never a mutation.

Entry point for ``skcapstone.fleet.card_slicing``, which was written and
tested with zero call sites (contract 1, docs/fleet/2026-09-18-learnings.md).
This module folds one card, projects it into the shape the recommender
consumes, and returns a JSON-safe report. It never creates, splits, claims,
or otherwise changes a card: decomposition changes the board's structure, so
acting on the recommendation stays a human or seat decision. The
CompositionVerificationContract, the proof obligation the parent retains
after all leaves complete, is always part of the report when leaves are
recommended.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from .card_store import CardStore
from .fleet.card_slicing import recommend_decomposition


def slice_preflight(home: Path, card_id: str, *, max_leaves: int = 5) -> dict[str, object]:
    """Return the decomposition recommendation for one card, JSON-safe.

    ``known`` is False for an unknown card. ``actuation`` is always
    ``recommendation-only`` so a log or transcript of the output can never be
    mistaken for evidence that a split happened.
    """
    store = CardStore(Path(home).expanduser())
    card = store.fold(card_id)
    if card is None:
        return {
            "card_id": card_id,
            "known": False,
            "reason": "unknown-card",
            "actuation": "recommendation-only",
        }
    meta = dict(card.meta or {})
    projection = {
        **meta,
        "id": card.id,
        "kind": card.kind.value,
        "title": card.title,
        "status": card.status.value,
        "owner": card.owner,
        "labels": list(card.labels),
        "acceptance_criteria": list(card.acceptance_criteria),
        "dependencies": list(card.dependencies),
        "links": dict(card.links or {}),
    }
    report = _jsonable(asdict(recommend_decomposition(projection, max_leaves=max_leaves)))
    report["card_id"] = card.id
    report["known"] = True
    report["actuation"] = "recommendation-only"
    return report


def _jsonable(value: object) -> object:
    """Recursively turn tuples into lists so json.dumps output is canonical."""
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
