"""Fenced host-local terminal capacity publication."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from skcoord.card_store import CardStore, card_mutation_lock


def _load_required(path: Path) -> dict[str, Any]:
    """Load one complete snapshot, failing closed on every ambiguity."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise ValueError("live snapshot is missing, unreadable, or malformed") from exc
    if not isinstance(value, dict):
        raise ValueError("live snapshot must be an object")
    cards = value.get("cards")
    workers = value.get("workers")
    if not isinstance(cards, list) or not all(isinstance(item, str) for item in cards):
        raise ValueError("live snapshot cards are malformed")
    if not isinstance(workers, list) or not all(isinstance(item, dict) for item in workers):
        raise ValueError("live snapshot workers are malformed")
    return value


def _generation_was_released(store: CardStore, card: str, owner: str, claim_revision: str) -> bool:
    """Require an exact release and no current or conflicting claim."""
    folded = store.fold(card)
    if folded is None or folded.owner is not None or folded.meta.get("_claim_revision"):
        return False
    if folded.meta.get("claim_conflicts"):
        return False
    return any(
        event.get("action") == "release_claim"
        and event.get("released_owner") == owner
        and event.get("expected_claim_revision") == claim_revision
        for event in store._read_events(card)
    )


def invalidate_worker(
    path: str | Path,
    host: str,
    card: str,
    owner: str,
    claim_revision: str,
) -> dict[str, Any]:
    """Remove one exact released generation while preserving every sibling."""
    target = Path(path)
    lock_path = target.with_name(target.name + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        import fcntl

        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        snapshot = _load_required(target)
        if snapshot.get("host") != host:
            raise ValueError("live snapshot host does not match terminal worker")
        expected = {
            "card_id": str(card),
            "owner": str(owner),
            "claim_revision": str(claim_revision),
        }
        workers = snapshot["workers"]
        if sum(item == expected for item in workers) != 1:
            raise ValueError("live snapshot lacks one exact worker generation")
        if str(card) not in snapshot["cards"]:
            raise ValueError("live snapshot lacks terminal card occupancy")
        snapshot["workers"] = [item for item in workers if item != expected]
        snapshot["cards"] = [item for item in snapshot["cards"] if item != str(card)]
        snapshot["invalidated_worker"] = expected
        snapshot["invalidated_at"] = time.time()
        payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return snapshot


def retire_worker_generation(
    path: str | Path,
    coordination_home: str | Path,
    host: str,
    card: str,
    owner: str,
    claim_revision: str,
) -> dict[str, Any] | None:
    """Fence CardStore release and snapshot invalidation under the card lock."""
    home = Path(coordination_home)
    store = CardStore(home)
    with card_mutation_lock(home, card):
        if not _generation_was_released(store, card, owner, claim_revision):
            return None
        return invalidate_worker(path, host, card, owner, claim_revision)
