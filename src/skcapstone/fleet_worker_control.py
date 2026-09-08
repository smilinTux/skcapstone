"""Fail-closed, claim-scoped control messages for one fleet worker."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

COMMANDS = frozenset({"status", "checkpoint", "graceful-cancel"})
FIELDS = frozenset(
    {
        "command",
        "card_id",
        "owner",
        "claim_revision",
        "host",
        "lane",
        "request_id",
        "expires_at",
    }
)
IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
CARD_RE = re.compile(r"^[0-9a-f]{8}$")
REQUEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
FORBIDDEN_RE = re.compile(
    r"(?i)(authorization|bearer|password|secret|api[_-]?key|access[_-]?token|"
    r"private[_-]?key|credential|capability)"
)
REDACT_RE = re.compile(
    r"(?i)(authorization:\s*(?:bearer|basic)\s+|"
    r"(?:api[_-]?key|access[_-]?token|password|secret|credential)\s*[=:]\s*)\S+"
)
TOKEN_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|[A-Za-z0-9_-]{32,})\b")


class ControlError(ValueError):
    """A control file was unsafe, stale, replayed, or did not match the claim."""


@dataclass(frozen=True)
class WorkerIdentity:
    """Immutable identity of one exact worker claim generation."""

    card_id: str
    owner: str
    claim_revision: str
    host: str
    lane: str


@dataclass(frozen=True)
class ControlCommand:
    """A validated control command."""

    command: str
    card_id: str
    owner: str
    claim_revision: str
    host: str
    lane: str
    request_id: str
    expires_at: str


def _utc(value: str) -> dt.datetime:
    """Parse an explicit timezone-aware ISO timestamp."""
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ControlError("invalid expiry") from exc
    if parsed.tzinfo is None:
        raise ControlError("expiry must include timezone")
    return parsed.astimezone(dt.timezone.utc)


def parse_control(
    raw: bytes,
    identity: WorkerIdentity,
    seen_request_ids: Iterable[str] = (),
    now: dt.datetime | None = None,
) -> ControlCommand:
    """Parse and fence a command to exactly one live worker identity."""
    if not raw or len(raw) > 4096 or b"\x00" in raw:
        raise ControlError("invalid control size")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlError("invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != FIELDS:
        raise ControlError("control fields do not match schema")
    if not all(isinstance(value, str) for value in payload.values()):
        raise ControlError("all control values must be strings")
    command = payload["command"]
    if command not in COMMANDS:
        raise ControlError("command is not allowed")
    if not CARD_RE.fullmatch(payload["card_id"]):
        raise ControlError("invalid card ID")
    for field in ("owner", "claim_revision", "host", "lane"):
        value = payload[field]
        if not IDENTITY_RE.fullmatch(value) or FORBIDDEN_RE.search(value):
            raise ControlError(f"invalid {field}")
    request_id = payload["request_id"]
    if not REQUEST_RE.fullmatch(request_id) or FORBIDDEN_RE.search(request_id):
        raise ControlError("invalid request ID")
    expected = asdict(identity)
    mismatches = [key for key, value in expected.items() if payload[key] != value]
    if mismatches:
        raise ControlError("control identity mismatch")
    if request_id in set(seen_request_ids):
        raise ControlError("replayed request ID")
    clock = now or dt.datetime.now(dt.timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=dt.timezone.utc)
    if _utc(payload["expires_at"]) <= clock.astimezone(dt.timezone.utc):
        raise ControlError("expired control")
    return ControlCommand(**payload)


def read_workspace_control(
    workspace: Path,
    identity: WorkerIdentity,
    seen_request_ids: Iterable[str] = (),
    now: dt.datetime | None = None,
) -> ControlCommand | None:
    """Read only ``workspace/control.json`` without following a symlink."""
    workspace = workspace.resolve(strict=True)
    path = workspace / "control.json"
    try:
        workspace_stat = workspace.stat()
        file_stat = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise ControlError("control file must be a regular file")
    if file_stat.st_uid != workspace_stat.st_uid or file_stat.st_uid != os.geteuid():
        raise ControlError("control file owner does not match worker workspace")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (file_stat.st_dev, file_stat.st_ino):
            raise ControlError("control file changed while opening")
        raw = os.read(fd, 4097)
    finally:
        os.close(fd)
    return parse_control(raw, identity, seen_request_ids, now)


def redact_terminal(text: str, limit: int = 2048) -> str:
    """Return bounded terminal text with credential-like material removed."""
    text = REDACT_RE.sub(lambda match: match.group(1) + "[REDACTED]", text)
    text = TOKEN_RE.sub("[REDACTED]", text)
    return text[-limit:]


def append_json_event(path: Path, event: dict[str, Any]) -> None:
    """Append one serializer-built event after parsing its serialized form."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    parsed = json.loads(line)
    if not isinstance(parsed, dict):
        raise ValueError("event must serialize to an object")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def completion_race(worker_completed: bool, pending_cancel: bool) -> str:
    """Resolve the only race deterministically, with completion taking priority."""
    if worker_completed:
        return "completed_cancel_voided" if pending_cancel else "completed"
    return "cancelled" if pending_cancel else "incomplete"


def reconcile_graceful_cancel(
    command: ControlCommand,
    *,
    worker_completed: bool,
    cgroup_is_empty: bool,
    current_claim: WorkerIdentity | None,
    evidence_path: Path,
    terminal: str,
    release_matching_claim: Any,
) -> str:
    """Reconcile a pending cancel after a turn and an authoritative claim reread.

    The caller must obtain ``current_claim`` from a fresh CardStore fold, not from
    launch-time state. Evidence is separate from the structural release callback.
    """
    outcome = completion_race(worker_completed, True)
    event = {
        "kind": "worker_control_evidence",
        "command": command.command,
        "request_id": command.request_id,
        "card_id": command.card_id,
        "owner": command.owner,
        "claim_revision": command.claim_revision,
        "host": command.host,
        "lane": command.lane,
        "outcome": outcome,
        "terminal": redact_terminal(terminal),
    }
    append_json_event(evidence_path, event)
    if worker_completed:
        return outcome
    if not cgroup_is_empty:
        raise ControlError("worker cgroup is not empty")
    expected = WorkerIdentity(
        command.card_id,
        command.owner,
        command.claim_revision,
        command.host,
        command.lane,
    )
    if current_claim != expected:
        raise ControlError("fresh CardStore claim does not match cancel")
    release_matching_claim(command.card_id, command.owner, command.claim_revision)
    return outcome
