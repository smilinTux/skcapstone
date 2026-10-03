"""Parent generation admission for queued owner helpers on every launch path."""

from __future__ import annotations

import re
from pathlib import Path

from skcoord.card_store import CardStore
from skcoord.owner_helpers import parent_contract_digest

from ..coord_helpers import _source
from ..jarvis_emergency import authorize_coord_mutation
from ..seat_boundaries import Action


def validate_helper_generation(home: Path, card) -> None:
    """Refuse stale helper custody; leave ordinary cards' admission unchanged."""
    if card is None:
        raise ValueError("helper admission requires a current card")
    parent_id = card.meta.get("helper_parent_id")
    labels = set(card.labels)
    if parent_id is None and "owner-helper" not in labels:
        return
    if (
        not isinstance(parent_id, str)
        or not re.fullmatch(r"[0-9a-f]{8}", parent_id)
        or parent_id == card.id
    ):
        raise ValueError("invalid owner-helper parent")
    parent = CardStore(home).fold(parent_id)
    if (
        parent is None
        or not parent.owner
        or parent.archived
        or parent.meta.get("voided")
        or parent.meta.get("claim_conflicts")
        or parent.status.value not in {"ready", "doing"}
        or parent.meta.get("helper_parent_id")
        or parent.kind.value != "task"
    ):
        raise ValueError("owner-helper parent is no longer active")
    if (
        card.meta.get("helper_parent_claim_revision") != parent.meta.get("_claim_revision")
        or not parent.meta.get("_claim_revision")
        or card.meta.get("helper_parent_contract_sha256") != parent_contract_digest(parent)
    ):
        raise ValueError("owner-helper parent generation changed")
    route = card.meta.get("logical_route")
    if route is None:
        # Preserve admission for helpers created before explicit route metadata.
        expected = {label for label in parent.labels if not label.lower().startswith("parent-")}
    else:
        if route not in {"sk-s", "sk-m"}:
            raise ValueError("owner-helper logical route is not bounded")
        expected = {label for label in parent.labels
                    if not re.fullmatch(r"sk-[a-z]+(?:-[a-z]+)?", label.strip().lower())}
        expected.add(route)
    expected |= {"owner-helper", "source-only", f"parent-{parent_id}"}
    if labels != expected or set(card.dependencies) != set(parent.dependencies):
        raise ValueError("owner-helper parent restrictions changed")
    if any(label.lower() == "review" or label.lower().startswith("seat-") for label in labels):
        raise ValueError("governed review cannot use owner-helper admission")
    if _source(parent) != _source(card):
        raise ValueError("owner-helper parent source changed")
    authorize_coord_mutation(parent.owner, Action.CREATE_CARD, parent_id, None, None)
