"""Fail-closed label and link writes for exact existing coordination cards."""

from __future__ import annotations

from pathlib import Path

from .card import CardEvent, CardEventLog


def require_card_id(card_id: object) -> str:
    """Return ``card_id`` if it has the shape ``coord create`` accepts, else ValueError.

    Reuses the create path's own pattern (optional three-letter kind prefix, then hex and
    hyphens, uppercase hex allowed, at most 64 chars) so live cards such as ``inc-e2ec1a1a``,
    ``rev-ff58cdd5`` and ``7a0c01`` stay annotatable. Garbage such as ``POOL_IDS|x`` is refused.
    """
    from .cli._validators import _TASK_ID_RE

    if (
        not isinstance(card_id, str)
        or not 0 < len(card_id) <= 64
        or _TASK_ID_RE.fullmatch(card_id) is None
    ):
        raise ValueError(
            "task_id must be a card id: optional three-letter kind prefix (inc-), "
            "then hex digits and hyphens, at most 64 characters"
        )
    return card_id


def append_coord_annotation(home: Path, event: CardEvent) -> None:
    """Validate an exact existing card before appending a label or link annotation."""
    from .card_store import CardStore

    require_card_id(event.card_id)
    card = CardStore(home).fold(event.card_id)
    if card is None or card.id != event.card_id:
        raise ValueError(f"CardStore card {event.card_id} has no foldable core")
    CardEventLog(home).append(event)
