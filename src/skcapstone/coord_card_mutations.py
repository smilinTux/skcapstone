"""Fail-closed label and link writes for exact existing coordination cards."""

from __future__ import annotations

import re
from pathlib import Path

from .card import CardEvent, CardEventLog
from .card_store import CardStore

_CARD_ID = re.compile(r"[0-9a-f]{8}")


def append_coord_annotation(home: Path, event: CardEvent) -> None:
    """Validate an exact existing card before appending an annotation."""
    if _CARD_ID.fullmatch(event.card_id) is None:
        raise ValueError("task_id must be exactly eight lowercase hexadecimal characters")
    card = CardStore(home).fold(event.card_id)
    if card is None or card.id != event.card_id:
        raise ValueError(f"CardStore card {event.card_id} has no foldable core")
    CardEventLog(home).append(event)
