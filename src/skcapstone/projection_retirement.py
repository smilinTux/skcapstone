"""Exact, reversible retirement for stale ownerless agent projections."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from skcoord.card_store import CardStore, card_mutation_lock
from skcoord.coordination import _board_mutation_lock
from skcoord.lifecycle import _lifecycle_lock

from .coord_amendments import is_voided
from .coordination import Board

_STALE_SECONDS = 15 * 60


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest of one file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def _append_manifest(path: Path, record: dict) -> None:
    """Append one durable quarantine event."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(record, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class RetirementReceipt:
    """Hash-bound result of one exact projection retirement."""

    home: Path
    projection_agent: str
    quarantined_path: Path
    projection_sha256: str
    receipt_sha256: str

    def restore(self, *, expected_quarantine_sha256: str) -> Path:
        """Restore this exact quarantined projection with hash fencing."""
        return restore_projection(
            self.home,
            projection_agent=self.projection_agent,
            expected_quarantine_sha256=expected_quarantine_sha256,
            expected_receipt_sha256=self.receipt_sha256,
            actor="receipt-restore",
        )


def retire_projection(
    home: Path,
    *,
    task_id: str,
    projection_agent: str,
    expected_card_sha256: str,
    expected_projection_sha256: str,
    actor: str,
) -> RetirementReceipt:
    """Quarantine one stale projection bound to one ownerless voided card."""
    root = Path(home)
    source = _projection_path(root, projection_agent)
    quarantine = root / "agents-quarantine"
    destination = quarantine / source.name
    manifest = quarantine / "manifest.jsonl"
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
        if not is_voided(root, task_id):
            raise ValueError("card must be voided")
        if not source.is_file() or source.is_symlink():
            raise ValueError("projection is missing or ambiguous")
        if _sha256(source) != expected_projection_sha256:
            raise ValueError("projection hash conflict")
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
            observed = datetime.fromisoformat(str(payload["last_seen"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("projection is malformed or has ambiguous liveness") from exc
        if payload.get("agent") != projection_agent:
            raise ValueError("projection identity does not match payload")
        if payload.get("current_task") != task_id:
            raise ValueError("projection task mismatch")
        if observed.tzinfo is None:
            raise ValueError("projection has ambiguous liveness")
        age = (datetime.now(timezone.utc) - observed).total_seconds()
        if age < _STALE_SECONDS:
            raise ValueError("projection is live or has future liveness")
        if destination.exists():
            raise ValueError("quarantine destination exists")

        record = {
            "event": "ownerless_projection_retired",
            "task_id": task_id,
            "projection_agent": projection_agent,
            "original_path": str(source),
            "quarantined_path": str(destination),
            "projection_sha256": expected_projection_sha256,
            "card_sha256": expected_card_sha256,
            "actor": actor,
            "retired_at": datetime.now(timezone.utc).isoformat(),
        }
        record["receipt_sha256"] = _canonical_sha256(record)
        quarantine.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)
        try:
            _append_manifest(manifest, record)
        except Exception:
            os.replace(destination, source)
            raise
        return RetirementReceipt(
            home=root,
            projection_agent=projection_agent,
            quarantined_path=destination,
            projection_sha256=expected_projection_sha256,
            receipt_sha256=record["receipt_sha256"],
        )


def restore_projection(
    home: Path,
    *,
    projection_agent: str,
    expected_quarantine_sha256: str,
    expected_receipt_sha256: str,
    actor: str,
) -> Path:
    """Restore one exact retired projection after verifying its receipt and hash."""
    root = Path(home)
    target = _projection_path(root, projection_agent)
    quarantine = root / "agents-quarantine"
    source = quarantine / target.name
    manifest = quarantine / "manifest.jsonl"
    with _board_mutation_lock(root), _lifecycle_lock(root, exclusive=True):
        if target.exists():
            raise ValueError("projection restore target exists")
        if not source.is_file() or source.is_symlink():
            raise ValueError("quarantined projection is missing or ambiguous")
        if _sha256(source) != expected_quarantine_sha256:
            raise ValueError("quarantine hash conflict")
        records = [json.loads(line) for line in manifest.read_text().splitlines() if line.strip()]
        matching = [
            item
            for item in records
            if item.get("event") == "ownerless_projection_retired"
            and item.get("projection_agent") == projection_agent
            and item.get("receipt_sha256") == expected_receipt_sha256
        ]
        recomputed_receipt_sha256 = (
            _canonical_sha256(
                {key: value for key, value in matching[0].items() if key != "receipt_sha256"}
            )
            if len(matching) == 1
            else None
        )
        if (
            len(matching) != 1
            or matching[0].get("projection_sha256") != expected_quarantine_sha256
            or recomputed_receipt_sha256 != expected_receipt_sha256
        ):
            raise ValueError("retirement receipt conflict")
        os.replace(source, target)
        try:
            _append_manifest(
                manifest,
                {
                    "event": "ownerless_projection_restored",
                    "projection_agent": projection_agent,
                    "projection_sha256": expected_quarantine_sha256,
                    "retirement_receipt_sha256": expected_receipt_sha256,
                    "actor": actor,
                    "restored_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            os.replace(target, source)
            raise
        return target
