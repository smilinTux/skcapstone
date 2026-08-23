"""Pending-decision store for the operator seat (Seat O5a).

The record store behind escalate-with-2-3-options and the approval tier.
Parks proposals as pending decisions, lists them, and resolves them (approve
one option, or reject) by a human. This module performs no actuation, only
file-backed record I/O; it is not a guardrail.
"""

from __future__ import annotations

import json
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


def park(
    store_dir: str | Path,
    options: list[dict[str, Any]],
    *,
    decision_id: str,
    created_iso: str,
) -> dict[str, Any]:
    """Park a proposal as a pending decision. Records only, no actuation.

    Args:
        store_dir: Directory the decision record is written into.
        options: One option for a simple approve/reject, or two to three
            options for an escalate-with-choices decision. Each option is a
            dict like ``{action, change_class, dry_run, rationale}``.
        decision_id: Unique id for this decision; also the record's filename.
        created_iso: ISO timestamp the decision was parked at.

    Returns:
        The pending record: ``{id, created, status, options, chosen,
        resolved_by, resolved_at}``.

    Raises:
        ValueError: ``options`` has fewer than 1 or more than 3 entries.
    """
    if not 1 <= len(options) <= 3:
        raise ValueError("park requires 1 to 3 options")
    path = _record_path(store_dir, decision_id)
    if path.exists():
        # Create-or-skip: a persistent firing re-parks the same (content-based) id,
        # so a standing issue is ONE decision the human resolves once, not a fresh
        # one every pass. The existing record and any resolution are preserved.
        return _load(path)
    record = {
        "id": decision_id,
        "created": created_iso,
        "status": "pending",
        "options": list(options),
        "chosen": None,
        "resolved_by": None,
        "resolved_at": None,
    }
    _dump(path, record)
    return record


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
