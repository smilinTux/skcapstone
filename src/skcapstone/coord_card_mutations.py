"""Fail-closed label and link writes for exact existing coordination cards."""

from __future__ import annotations

import re
from pathlib import Path

from .card import CardEvent, CardEventLog

_CARD_ID = re.compile(r"[0-9a-f]{8}")


def require_card_id(card_id: object) -> str:
    """Return ``card_id`` if it is exactly eight lowercase hex characters, else ValueError."""
    if not isinstance(card_id, str) or _CARD_ID.fullmatch(card_id) is None:
        raise ValueError("task_id must be exactly eight lowercase hexadecimal characters")
    return card_id


def append_coord_annotation(home: Path, event: CardEvent) -> None:
    """Validate an exact existing card before appending a label or link annotation."""
    from .card_store import CardStore

    require_card_id(event.card_id)
    card = CardStore(home).fold(event.card_id)
    if card is None or card.id != event.card_id:
        raise ValueError(f"CardStore card {event.card_id} has no foldable core")
    CardEventLog(home).append(event)
