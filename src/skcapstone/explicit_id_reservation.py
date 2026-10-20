"""Durable reservations for rejected caller-supplied coordination IDs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from skcoord.card import _swimlane_for_tags
from skcoord.card_store import (
    CardCore,
    CardStore,
    _open_coordination_child_directory,
    _open_lockfile,
    validate_card_lock_identifier,
)

_LOG_PREFIX = "card-creation-attempts"


def request_digest(request: dict[str, Any]) -> str:
    """Return the stable SHA-256 digest for one explicit-ID request."""
    payload = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _records(home: Path) -> list[dict[str, Any]]:
    """Read durable creation-attempt records, ignoring torn trailing lines."""
    recovery = Path(home).expanduser() / "coordination" / "recovery"
    records: list[dict[str, Any]] = []
    if not recovery.is_dir():
        return records
    for path in recovery.glob(f"{_LOG_PREFIX}*.jsonl"):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            continue
    return records


def _rejected(home: Path, card_id: str) -> bool:
    """Return whether ``card_id`` has a durable rejected attempt."""
    return any(
        record.get("card_id") == card_id and record.get("outcome") == "rejected"
        for record in _records(home)
    )


def _append_rejection(home: Path, card_id: str, digest: str, actor: str, reason: str) -> None:
    """Append one rejection while the creation governor is held."""
    recovery_fd = _open_coordination_child_directory(home, "recovery")
    filename = f"{_LOG_PREFIX}@{socket.gethostname()}.jsonl"
    fd = -1
    try:
        fd = os.open(
            filename,
            os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=recovery_fd,
        )
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("creation-attempt log is unsafe")
        record = {
            "card_id": card_id,
            "request_digest": digest,
            "actor": actor,
            "ts": datetime.now(timezone.utc).isoformat(),
            "outcome": "rejected",
            "reason": reason,
        }
        os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(fd)
        os.fsync(recovery_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(recovery_fd)


def reserve_rejection(
    home: Path,
    card_id: str,
    request: dict[str, Any],
    actor: str,
    reason: str,
) -> None:
    """Durably reserve a rejected explicit ID unless a card already won."""
    validate_card_lock_identifier(card_id)
    store = CardStore(home)
    with _open_lockfile(home, "card-creation-governor.lock", "card creation") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if store._load_core(card_id) is None and not _rejected(home, card_id):
                _append_rejection(home, card_id, request_digest(request), actor, reason)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def assert_available(home: Path, card_id: str) -> None:
    """Reject reuse of an ID reserved by a failed creation attempt."""
    if _rejected(home, card_id):
        raise ValueError(f"Card ID {card_id} is reserved by a rejected creation attempt")


def _core_for_task(
    task: Any, owner: str | None = None, claim_revision: str | None = None
) -> CardCore:
    """Translate a coordination task to its immutable CardStore core."""
    tags_lower = {tag.lower() for tag in task.tags}
    return CardCore(
        id=task.id,
        kind="epic" if "epic" in tags_lower else "task",
        title=task.title,
        description=task.description,
        created_by=task.created_by,
        created_at=task.created_at,
        acceptance_criteria=list(getattr(task, "acceptance_criteria", []) or []),
        dependencies=list(task.dependencies),
        initial_priority=task.priority.value,
        initial_swimlane=_swimlane_for_tags(task.tags),
        initial_labels=list(task.tags),
        initial_owner=owner,
        initial_claim_revision=claim_revision,
        meta=dict(task.meta),
    )


def _write_core(store: CardStore, core: CardCore) -> None:
    """Write one immutable core while the caller holds the governor lock."""
    card_fd = store._open_card_directory(core.id)
    fd = -1
    try:
        fd = os.open(
            "core.json",
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o644,
            dir_fd=card_fd,
        )
        payload = (core.model_dump_json(indent=2) + "\n").encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(fd, payload[offset:])
        os.fsync(fd)
    finally:
        if fd >= 0:
            os.close(fd)
        os.fsync(card_fd)
        os.close(card_fd)


def create_explicit_task(board: Any, task: Any, request: dict[str, Any], actor: str) -> Path:
    """Create an explicit-ID task atomically against rejection reservations."""
    store = CardStore(board.home)
    core = _core_for_task(task)
    with _open_lockfile(board.home, "card-creation-governor.lock", "card creation") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            assert_available(board.home, task.id)
            if store._load_core(task.id) is None:
                try:
                    store._govern_create(core)
                except ValueError as exc:
                    _append_rejection(
                        board.home, task.id, request_digest(request), actor, str(exc)
                    )
                    raise
                _write_core(store, core)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    store._ensure_card_lock_anchor(task.id)
    return board.create_task(task)


def create_claimed_explicit_task(
    board: Any,
    task: Any,
    owner: str,
    request: dict[str, Any],
    actor: str,
) -> tuple[Path, str]:
    """Create and claim an explicit-ID task after atomically winning its ID."""
    store = CardStore(board.home)
    revision = uuid.uuid4().hex
    core = _core_for_task(task, owner, revision)
    with _open_lockfile(board.home, "card-creation-governor.lock", "card creation") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            assert_available(board.home, task.id)
            existing = store._load_core(task.id)
            if existing is None:
                try:
                    store._govern_create(core)
                except ValueError as exc:
                    _append_rejection(
                        board.home, task.id, request_digest(request), actor, str(exc)
                    )
                    raise
                _write_core(store, core)
            else:
                stored = CardCore.model_validate(existing)
                if stored.initial_owner != owner or not stored.initial_claim_revision:
                    raise ValueError(f"CardStore create-and-claim conflict for {task.id}")
                revision = stored.initial_claim_revision
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    store._ensure_card_lock_anchor(task.id)
    return board.create_claimed_task(task, owner)
