"""Governed revival of one completed coordination card."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from skcoord.card import Card, Column
from skcoord.card_store import CardStore, card_mutation_lock


def _state_sha256(card: Card) -> str:
    """Return a deterministic fingerprint of the folded precondition."""
    payload = json.dumps(
        card.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _require_reopenable(card: Card | None, task_id: str) -> Card:
    """Return CARD only when it is the exact terminal state reopen supports."""
    if card is None:
        raise ValueError(f"CardStore card {task_id} has no foldable core")
    if card.meta.get("voided"):
        raise ValueError(f"card {task_id} is voided and cannot be reopened")
    if card.archived:
        raise ValueError(f"card {task_id} is archived and cannot be reopened")
    if card.owner:
        raise ValueError(f"card {task_id} is owned by {card.owner} and cannot be reopened")
    if card.status != Column.DONE:
        raise ValueError(f"card {task_id} is {card.status.value}, not terminal done")
    return card


def reopen_card(home: Path, task_id: str, reason: str, agent: str) -> dict:
    """Append one preconditioned reopen event and preserve prior history."""
    reason = reason.strip()
    if not reason:
        raise ValueError("a reopen reason is required")

    home = Path(home).expanduser()
    store = CardStore(home)
    expected = _require_reopenable(store.fold(task_id), task_id)
    expected_sha256 = _state_sha256(expected)

    with card_mutation_lock(home, task_id):
        current = store.fold(task_id)
        if current is None or _state_sha256(current) != expected_sha256:
            raise ValueError(f"card {task_id} changed while acquiring the reopen lock")
        _require_reopenable(current, task_id)
        return store.append_event(
            task_id,
            "reopen",
            agent,
            column=Column.BACKLOG.value,
            reason=reason,
            previous_state_sha256=expected_sha256,
        )
