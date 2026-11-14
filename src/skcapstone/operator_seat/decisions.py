"""Pending-decision store for the operator seat (Seat O5a).

The record store behind ATLAS escalations. Every ATLAS escalation is report-only
(card f8dd9441): a record here is acknowledge-only and authorizes nothing. Parks
proposals as pending decisions, one record per episode, lists them, and resolves
them by a human. This module performs no actuation, only
file-backed record I/O; it is not a guardrail.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..atomic_io import atomic_write_text


def _record_path(store_dir: str | Path, decision_id: str) -> Path:
    return Path(store_dir) / f"{decision_id}.json"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(record, indent=2, sort_keys=True) + "\n")


def _current_episode(store_dir: str | Path, base_id: str) -> tuple[str, int]:
    """The newest episode record of ``base_id``: ``(id, n)``, with n == 1 for the base."""
    best_id, best_n = base_id, 1
    for path in Path(store_dir).glob(f"{base_id}-e*.json"):
        suffix = path.stem[len(base_id) + 2 :]
        if suffix.isdigit() and int(suffix) > best_n:
            best_id, best_n = path.stem, int(suffix)
    return best_id, best_n


def park(
    store_dir: str | Path,
    options: list[dict[str, Any]],
    *,
    decision_id: str,
    created_iso: str,
) -> dict[str, Any]:
    """Park a proposal as a pending decision. Records only, no actuation.

    Create-or-skip per EPISODE: a persistent firing re-parks the same content-based id, so
    one episode is ONE record a human acknowledges once. When that record is resolved AND its
    episode has ended (``end_cleared_episodes`` saw the condition definitely cleared), the
    next firing is a new episode, parked as ``<decision_id>-e<n>``. Nothing is rewritten.

    Args:
        store_dir: Directory the decision record is written into.
        options: One to three option dicts (``{app, condition, object, action, ...}``).
        decision_id: Content-based id of the proposal; the first episode's filename.
        created_iso: ISO timestamp the decision was parked at.

    Returns:
        The current episode's record: ``{id, created, status, options, chosen,
        resolved_by, resolved_at}``.

    Raises:
        ValueError: ``options`` has fewer than 1 or more than 3 entries.
    """
    if not 1 <= len(options) <= 3:
        raise ValueError("park requires 1 to 3 options")
    current_id, episode = _current_episode(store_dir, decision_id)
    path = _record_path(store_dir, current_id)
    if path.exists():
        existing = _load(path)
        if existing.get("status") == "pending" or not existing.get("episode_ended_at"):
            return existing
        current_id = f"{decision_id}-e{episode + 1}"
    record = {
        "id": current_id,
        "created": created_iso,
        "status": "pending",
        "options": list(options),
        "chosen": None,
        "resolved_by": None,
        "resolved_at": None,
    }
    _dump(_record_path(store_dir, current_id), record)
    return record


def end_cleared_episodes(
    store_dir: str | Path,
    cleared: Callable[[dict[str, Any]], bool],
    *,
    ended_iso: str,
    node: str | None = None,
) -> list[str]:
    """Mark resolved decisions whose condition now reads definitely cleared as ended.

    ``cleared(option)`` answers from the current observation. Pending records are left
    alone, so an unacknowledged episode is never replaced. With ``node``, only records
    whose every option was observed on that node (``observed_on``) are judged: the store
    is replicated, and one node's view must not end another node's episode. Returns the
    ids marked.
    """
    directory = Path(store_dir)
    if not directory.exists():
        return []
    marked: list[str] = []
    for path in sorted(directory.glob("*.json")):
        try:
            record = _load(path)
        except (OSError, ValueError):
            continue
        if record.get("status") == "pending" or record.get("episode_ended_at"):
            continue
        options = [o for o in record.get("options", []) if isinstance(o, dict)]
        if node is not None and any(o.get("observed_on") != node for o in options):
            continue
        if options and all(cleared(o) for o in options):
            record["episode_ended_at"] = ended_iso
            _dump(path, record)
            marked.append(record.get("id", path.stem))
    return marked


def list_pending(store_dir: str | Path) -> list[dict[str, Any]]:
    """List pending decisions, sorted by creation time.

    Args:
        store_dir: Directory to read decision records from.

    Returns:
        Records with ``status == "pending"``, sorted by ``created``.
    """
    dir_path = Path(store_dir)
    if not dir_path.exists():
        return []
    records = [_load(p) for p in sorted(dir_path.glob("*.json"))]
    pending = [r for r in records if r["status"] == "pending"]
    pending.sort(key=lambda r: r["created"])
    return pending


def resolve(
    store_dir: str | Path,
    decision_id: str,
    *,
    approve: bool,
    choice: int | None,
    by: str,
    resolved_iso: str,
) -> dict[str, Any]:
    """Resolve a pending decision by a human. No actuation, records only.

    Args:
        store_dir: Directory the decision record lives in.
        decision_id: Id of the decision to resolve.
        approve: True to approve one option, False to reject.
        choice: Index into ``options`` to approve. Required and validated
            against the option count when there is more than one option;
            ignored otherwise (the single option is chosen).
        by: Who resolved the decision. Must not be ``"operator"``: only a
            human may resolve.
        resolved_iso: ISO timestamp the decision was resolved at.

    Returns:
        The updated record.

    Raises:
        ValueError: ``by`` is ``"operator"``, the decision id is unknown,
            the decision was already resolved, or ``choice`` is out of
            range for a multi-option decision.
    """
    if by == "operator":
        raise ValueError("only a human may resolve a pending decision")

    path = _record_path(store_dir, decision_id)
    if not path.exists():
        raise ValueError(f"unknown decision: {decision_id}")

    record = _load(path)
    if record["status"] != "pending":
        raise ValueError(f"decision already resolved: {decision_id}")

    if approve:
        options = record["options"]
        if len(options) > 1:
            if choice is None or not 0 <= choice < len(options):
                raise ValueError(f"choice out of range for {len(options)} options")
            record["chosen"] = choice
        else:
            record["chosen"] = 0
        record["status"] = "approved"
    else:
        record["status"] = "rejected"
        record["chosen"] = None

    record["resolved_by"] = by
    record["resolved_at"] = resolved_iso
    _dump(path, record)
    return record
