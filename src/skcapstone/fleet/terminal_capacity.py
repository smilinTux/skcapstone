"""Host-local terminal capacity publication.

Snapshots are evidence, not CardStore records. A release is accepted only when
its immutable worker identity still owns the advertised seat. Invalid or
ambiguous observations deliberately leave the last snapshot untouched.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _identity_matches(snapshot: dict[str, Any], card: str, identity: dict[str, str]) -> bool:
    if not identity:
        return True
    generations = snapshot.get("generations")
    if not isinstance(generations, dict):
        return False
    record = generations.get(str(card))
    if not isinstance(record, dict):
        return False
    return all(str(record.get(key)) == value for key, value in identity.items())


def invalidate_worker(
    path: str | Path,
    host: str,
    card: str,
    *,
    owner: str | None = None,
    claim_revision: str | None = None,
    card_id: str | None = None,
    process_evidence: bool | None = None,
    cgroup_evidence: bool | None = None,
) -> dict[str, Any]:
    """Invalidate one exact worker generation, or make no publication.

    ``owner``, ``claim_revision`` and ``card_id`` fence reuse of a card ID. When
    process or cgroup evidence is supplied, both must be true; an absent or
    contradictory observation is occupied, never free. Legacy callers without
    identity arguments retain the atomic remove behavior.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    identity = {"card_id": str(card_id or card), "owner": str(owner),
                "claim_revision": str(claim_revision)} if any(
                    value is not None for value in (owner, claim_revision, card_id)
                ) else {}
    with (target.with_name(target.name + ".lock")).open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        snapshot = _load(target)
        if snapshot is None or not isinstance(snapshot.get("cards"), list):
            return {}
        if not _identity_matches(snapshot, str(card), identity):
            return snapshot
        if (process_evidence is not None or cgroup_evidence is not None) and not (
            process_evidence is True and cgroup_evidence is True
        ):
            return snapshot
        cards = [item for item in snapshot["cards"] if str(item) != str(card)]
        snapshot = dict(snapshot)
        snapshot["host"] = host
        snapshot["cards"] = cards
        snapshot["invalidated_card"] = str(card)
        snapshot["invalidated_at"] = time.time()
        if isinstance(snapshot.get("generations"), dict):
            snapshot["generations"] = dict(snapshot["generations"])
            snapshot["generations"].pop(str(card), None)
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
