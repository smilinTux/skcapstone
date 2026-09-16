"""Fail-closed compatibility adapter for immutable agent retirement."""

from __future__ import annotations

import hashlib
import json
from importlib import import_module
from pathlib import Path

from skcoord.card_store import CardStore, card_mutation_lock
from skcoord.coordination import _board_mutation_lock
from skcoord.lifecycle import _lifecycle_lock

from .coord_amendments import is_voided
from .coordination import Board


def _canonical_sha256(value: dict) -> str:
    """Hash one JSON-compatible mapping canonically."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def card_generation_sha256(card) -> str:
    """Return a stable digest for one folded card generation."""
    if card is None:
        raise ValueError("card is missing")
    return _canonical_sha256(card.model_dump(mode="json"))


def _projection_path(home: Path, projection_agent: str) -> Path:
    """Resolve one canonical projection path without allowing traversal."""
    if not projection_agent or any(bad in projection_agent for bad in (".", "/", "\\")):
        raise ValueError("projection identity is not canonical")
    path = Board(home).agents_dir / f"{projection_agent}.json"
    if path.name != f"{projection_agent}.json":
        raise ValueError("projection identity is not canonical")
    return path


def _require_unambiguous_projection(path: Path) -> None:
    """Refuse a canonical cache with unresolved Syncthing sibling bytes."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("projection is unsafe or ambiguous")
    if any(path.parent.glob(f"{path.stem}.sync-conflict-*.json")):
        raise ValueError("projection generation is ambiguous")


def _generation_broker():
    """Load the SKCoord authority, if this installation supports generations."""
    try:
        broker = import_module("skcoord.projection_generation")
    except ModuleNotFoundError as exc:
        if exc.name != "skcoord.projection_generation":
            raise
        return None
    return broker if getattr(broker, "IMMUTABLE_GENERATION_CONTRACT_VERSION", None) == 1 else None


def retire_projection(
    home: Path,
    *,
    task_id: str,
    projection_agent: str,
    expected_card_sha256: str,
    expected_projection_sha256: str,
    actor: str,
):
    """Retire one exact immutable generation through the SKCoord authority."""
    root = Path(home)
    source = _projection_path(root, projection_agent)
    with (
        _board_mutation_lock(root),
        card_mutation_lock(root, task_id),
        _lifecycle_lock(root, exclusive=True),
    ):
        card = CardStore(root).fold(task_id)
        if card_generation_sha256(card) != expected_card_sha256:
            raise ValueError("card hash conflict")
        if card.owner is not None:
            raise ValueError("card must be ownerless")
        if not card.archived or card.status.value == "done":
            raise ValueError("card must be archived and not done")
        if not is_voided(root, task_id):
            raise ValueError("card must be voided")
        _require_unambiguous_projection(source)
        broker = _generation_broker()
        if broker is None or not callable(getattr(broker, "retire_projection", None)):
            raise RuntimeError("immutable generation broker unavailable")
        return broker.retire_projection(
            root,
            task_id=task_id,
            projection_agent=projection_agent,
            expected_card_sha256=expected_card_sha256,
            expected_generation_sha256=expected_projection_sha256,
            actor=actor,
        )


def restore_projection(
    home: Path,
    *,
    projection_agent: str,
    expected_quarantine_sha256: str,
    expected_receipt_sha256: str,
    actor: str,
):
    """Restore through the immutable generation authority, never the cache."""
    root = Path(home)
    target = _projection_path(root, projection_agent)
    with _board_mutation_lock(root), _lifecycle_lock(root, exclusive=True):
        _require_unambiguous_projection(target)
        broker = _generation_broker()
        if broker is None or not callable(getattr(broker, "restore_projection", None)):
            raise RuntimeError("immutable generation broker unavailable")
        return broker.restore_projection(
            root,
            projection_agent=projection_agent,
            expected_generation_sha256=expected_quarantine_sha256,
            expected_receipt_sha256=expected_receipt_sha256,
            actor=actor,
        )
