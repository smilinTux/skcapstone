"""Durable, exact-process child progress observations for SKFleet."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import select
import signal
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from .worker_watchdog import (
    ChildLeaseConfig,
    ChildLeaseObservation,
    ChildLeaseReceipt,
    evaluate_child_lease,
)

_CARD_RE = re.compile(r"[0-9a-f]{8}")
_OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,126}[A-Za-z0-9])?")
_LANE_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
SNAPSHOT_VERSION = 1


def _required_text(value: object, name: str, pattern: re.Pattern[str] | None = None) -> str:
    """Return one bounded non-empty identity field."""
    text = str(value or "")
    if not text or len(text) > 256 or (pattern is not None and not pattern.fullmatch(text)):
        raise ValueError(f"invalid child progress {name}")
    return text


def process_start_ticks(pid: int) -> int:
    """Read Linux process start ticks without confusing spaces in comm."""
    if not isinstance(pid, int) or pid <= 0:
        raise ValueError("invalid child PID")
    raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    _prefix, separator, suffix = raw.rpartition(")")
    fields = suffix.strip().split() if separator else []
    if len(fields) < 20:
        raise ValueError("invalid process stat")
    ticks = int(fields[19])
    if ticks <= 0:
        raise ValueError("invalid process start ticks")
    return ticks


def process_cgroup(pid: int) -> str:
    """Read the unified cgroup for one Linux process."""
    rows = (Path("/proc") / str(pid) / "cgroup").read_text(encoding="utf-8").splitlines()
    groups = [row[3:] for row in rows if row.startswith("0::")]
    if len(groups) != 1 or not groups[0].startswith("/"):
        raise ValueError("invalid process cgroup")
    return groups[0]


def process_environment(pid: int) -> dict[str, str]:
    """Read only the four non-secret fleet identity variables."""
    allowed = {
        b"SKAGENT": "owner",
        b"SKFLEET_CARD_ID": "card_id",
        b"SKFLEET_CLAIM_REVISION": "claim_revision",
        b"SKFLEET_SESSION_ID": "session_id",
    }
    result: dict[str, str] = {}
    raw = (Path("/proc") / str(pid) / "environ").read_bytes()
    for entry in raw.split(b"\0"):
        key, separator, value = entry.partition(b"=")
        if separator and key in allowed:
            result[allowed[key]] = os.fsdecode(value)
    return result


@dataclass(frozen=True)
class ChildProgressSnapshot:
    """One persisted observation for an exact worker child generation."""

    card_id: str
    owner: str
    claim_revision: str
    host: str
    lane: str
    model_bucket: str
    session_id: str
    unit: str
    control_group: str
    wrapper_pid: int
    child_pid: int
    child_start_ticks: int
    started_at: float
    started_at_utc: str
    observed_at: float
    startup_complete_at: float | None
    provider_started_at: float | None
    first_output_at: float | None
    last_progress_at: float | None
    stdout_bytes: int
    child_alive: bool
    human_gate: bool
    side_effects: bool
    config: ChildLeaseConfig
    version: int = SNAPSHOT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.config, ChildLeaseConfig):
            raise ValueError("invalid child progress configuration")
        _required_text(self.card_id, "card", _CARD_RE)
        _required_text(self.owner, "owner", _OWNER_RE)
        _required_text(self.claim_revision, "claim revision")
        _required_text(self.host, "host")
        _required_text(self.lane, "lane", _LANE_RE)
        _required_text(self.model_bucket, "model bucket")
        _required_text(self.session_id, "session")
        _required_text(self.unit, "unit")
        started_at_utc = _required_text(self.started_at_utc, "UTC start time")
        try:
            parsed_start = datetime.fromisoformat(started_at_utc.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid child progress UTC start time") from exc
        if parsed_start.tzinfo is None:
            raise ValueError("invalid child progress UTC start time")
        if not self.control_group.startswith("/") or ".." in self.control_group.split("/"):
            raise ValueError("invalid child progress control group")
        if type(self.version) is not int or self.version != SNAPSHOT_VERSION:
            raise ValueError("unsupported child progress snapshot version")
        if any(
            type(value) is not int or value <= 0
            for value in (self.wrapper_pid, self.child_pid, self.child_start_ticks)
        ):
            raise ValueError("invalid child progress process identity")
        if type(self.stdout_bytes) is not int or self.stdout_bytes < 0:
            raise ValueError("invalid child progress counters")
        if any(
            type(value) is not bool
            for value in (self.child_alive, self.human_gate, self.side_effects)
        ):
            raise ValueError("invalid child progress policy flags")
        if any(
            not isinstance(value, (int, float)) or not math.isfinite(value)
            for value in (self.started_at, self.observed_at)
        ):
            raise ValueError("invalid child progress clock")
        if self.observed_at < self.started_at:
            raise ValueError("invalid child progress counters")
        marks = (
            self.startup_complete_at,
            self.provider_started_at,
            self.first_output_at,
            self.last_progress_at,
        )
        if any(
            mark is not None
            and (
                not isinstance(mark, (int, float))
                or not math.isfinite(mark)
                or not self.started_at <= mark <= self.observed_at
            )
            for mark in marks
        ):
            raise ValueError("invalid child progress timestamp")
        expected_unit = f"skfleet-worker-{self.lane}-{self.card_id}.service"
        if self.unit != expected_unit:
            raise ValueError("child progress unit identity mismatch")

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "ChildProgressSnapshot":
        """Validate and load a persisted snapshot."""
        expected = {
            "version",
            "card_id",
            "owner",
            "claim_revision",
            "host",
            "lane",
            "model_bucket",
            "session_id",
            "unit",
            "control_group",
            "wrapper_pid",
            "child_pid",
            "child_start_ticks",
            "started_at",
            "started_at_utc",
            "observed_at",
            "startup_complete_at",
            "provider_started_at",
            "first_output_at",
            "last_progress_at",
            "stdout_bytes",
            "child_alive",
            "human_gate",
            "side_effects",
            "config",
        }
        if set(values) != expected or not isinstance(values.get("config"), Mapping):
            raise ValueError("child progress snapshot fields are incomplete")
        payload = dict(values)
        payload["config"] = ChildLeaseConfig.from_mapping(values["config"])
        return cls(**payload)  # type: ignore[arg-type]

    def as_dict(self) -> dict[str, object]:
        """Return canonical JSON-safe snapshot data."""
        payload = asdict(self)
        payload["config"] = self.config.as_dict()
        return payload

    def identity(self) -> tuple[str, str, str, int, int]:
        """Return the exact generation and process fence."""
        return (
            self.card_id,
            self.owner,
            self.claim_revision,
            self.child_pid,
            self.child_start_ticks,
        )


def snapshot_key(card_id: str, owner: str, claim_revision: str) -> str:
    """Return the stable filename key for one worker generation."""
    raw = f"{card_id}\0{owner}\0{claim_revision}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _canonical_bytes(payload: Mapping[str, object]) -> bytes:
    """Serialize one bounded canonical JSON object."""
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(encoded) > 64 * 1024:
        raise ValueError("child progress record exceeds 64 KiB")
    json.loads(encoded)
    return encoded


def write_snapshot(path: Path, snapshot: ChildProgressSnapshot) -> str:
    """Atomically persist the latest observation and return its hash."""
    encoded = _canonical_bytes(snapshot.as_dict())
    digest = hashlib.sha256(encoded).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return digest


def load_snapshot(path: Path) -> ChildProgressSnapshot:
    """Read and validate one complete snapshot."""
    raw = path.read_bytes()
    if len(raw) > 64 * 1024 or not raw.endswith(b"\n"):
        raise ValueError("invalid child progress snapshot bytes")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("child progress snapshot is not an object")
    return ChildProgressSnapshot.from_mapping(payload)


def write_receipt(directory: Path, payload: Mapping[str, object]) -> tuple[Path, str]:
    """Persist one immutable idempotent receipt and return its content hash."""
    encoded = _canonical_bytes(payload)
    digest = hashlib.sha256(encoded).hexdigest()
    receipt_id = str(payload.get("receipt_id") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", receipt_id):
        raise ValueError("child progress receipt ID is invalid")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{receipt_id}.json"
    temporary = directory / (
        f".{receipt_id}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            existing = path.read_bytes()
            parsed = json.loads(existing)
            if not isinstance(parsed, dict) or parsed.get("receipt_id") != receipt_id:
                raise ValueError("child progress receipt ID collision")
            digest = hashlib.sha256(existing).hexdigest()
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return path, digest


def exact_child_matches(snapshot: ChildProgressSnapshot) -> bool:
    """Require PID generation, cgroup, and all fleet identity variables."""
    try:
        return (
            process_start_ticks(snapshot.child_pid) == snapshot.child_start_ticks
            and process_cgroup(snapshot.child_pid) == snapshot.control_group
            and process_environment(snapshot.child_pid)
            == {
                "owner": snapshot.owner,
                "card_id": snapshot.card_id,
                "claim_revision": snapshot.claim_revision,
                "session_id": snapshot.session_id,
            }
        )
    except (OSError, UnicodeError, ValueError):
        return False


def cancel_exact_child(
    snapshot: ChildProgressSnapshot,
    *,
    terminate_timeout_s: float = 10.0,
    kill_timeout_s: float = 2.0,
) -> str:
    """Boundedly stop only the pidfd-bound exact child process."""
    if terminate_timeout_s <= 0 or kill_timeout_s <= 0:
        raise ValueError("child cancellation timeouts must be positive")
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("pidfd cancellation is unavailable")
    try:
        pidfd = os.pidfd_open(snapshot.child_pid, 0)
    except ProcessLookupError:
        return "already-exited"
    try:
        if not exact_child_matches(snapshot):
            raise RuntimeError("exact child identity changed")
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        if poller.poll(round(terminate_timeout_s * 1000)):
            return "terminated"
        if not exact_child_matches(snapshot):
            raise RuntimeError("exact child identity changed before kill")
        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        if poller.poll(round(kill_timeout_s * 1000)):
            return "killed"
        raise TimeoutError("exact child did not exit within bounded timeout")
    finally:
        os.close(pidfd)


def lease_receipts(
    snapshot: ChildProgressSnapshot,
    *,
    now: float,
    terminal: bool = False,
    superseded: bool = False,
    ambiguous_progress: bool = False,
) -> tuple[ChildLeaseReceipt, ...]:
    """Evaluate every phase reached by the live worker snapshot."""
    base = {
        "card": snapshot.card_id,
        "owner": snapshot.owner,
        "claim_revision": snapshot.claim_revision,
        "host": snapshot.host,
        "lane": snapshot.lane,
        "model_bucket": snapshot.model_bucket,
        "started_at": snapshot.started_at,
        "startup_complete_at": snapshot.startup_complete_at,
        "first_output_at": snapshot.first_output_at,
        "last_output_at": snapshot.last_progress_at,
        "provider_started_at": snapshot.provider_started_at,
        "last_progress_at": snapshot.last_progress_at,
        "child_alive": snapshot.child_alive,
        "side_effects": snapshot.side_effects,
        "human_gate": snapshot.human_gate,
        "terminal": terminal,
        "superseded": superseded,
        "ambiguous_progress": ambiguous_progress,
        "child_pid": snapshot.child_pid,
        "child_start_ticks": snapshot.child_start_ticks,
    }
    phases = ["startup"]
    if snapshot.startup_complete_at is not None:
        phases.append("first-output")
    if snapshot.provider_started_at is not None:
        phases.append("provider-response")
    if snapshot.first_output_at is not None:
        phases.append("progress")
    return tuple(
        evaluate_child_lease(
            ChildLeaseObservation(phase=phase, **base),
            now=now,
            config=snapshot.config,
        )
        for phase in phases
    )


def active_receipt(
    snapshot: ChildProgressSnapshot, receipts: tuple[ChildLeaseReceipt, ...]
) -> ChildLeaseReceipt:
    """Choose the current phase without hiding an earlier expired lease."""
    by_phase = {receipt.phase: receipt for receipt in receipts}
    if snapshot.startup_complete_at is None:
        return by_phase["startup"]
    if snapshot.first_output_at is None:
        provider = by_phase.get("provider-response")
        if provider is not None and provider.state in {"child-stalled", "not-replayable"}:
            return provider
        return by_phase["first-output"]
    return by_phase["progress"]


def receipt_payload(
    snapshot: ChildProgressSnapshot,
    receipt: ChildLeaseReceipt,
    *,
    stage: str,
    mode: str,
    outcome: str,
    retry_disposition: str,
) -> dict[str, object]:
    """Build a non-secret receipt bound to config and exact process identity."""
    if stage not in {"assessment", "pre-action", "post-action"}:
        raise ValueError("invalid child progress receipt stage")
    if mode not in {"observe", "act"}:
        raise ValueError("invalid child progress mode")
    if retry_disposition not in {"retryable", "forbidden", "unchanged"}:
        raise ValueError("invalid child progress retry disposition")
    config = snapshot.config.as_dict()
    config_hash = hashlib.sha256(_canonical_bytes(config)).hexdigest()
    anchors = {
        "startup": snapshot.started_at,
        "first-output": snapshot.startup_complete_at or snapshot.started_at,
        "provider-response": snapshot.provider_started_at or snapshot.started_at,
        "progress": snapshot.last_progress_at or snapshot.started_at,
    }
    event_at = anchors[receipt.phase]
    if receipt.state == "child-stalled":
        event_at += receipt.lease_s
    started_utc = datetime.fromisoformat(snapshot.started_at_utc.replace("Z", "+00:00"))
    recorded_at = started_utc + timedelta(seconds=max(0.0, event_at - snapshot.started_at))
    stable_identity = {
        "card_id": snapshot.card_id,
        "owner": snapshot.owner,
        "claim_revision": snapshot.claim_revision,
        "host": snapshot.host,
        "lane": snapshot.lane,
        "model_bucket": snapshot.model_bucket,
        "session_id": snapshot.session_id,
        "unit": snapshot.unit,
        "control_group": snapshot.control_group,
        "wrapper_pid": snapshot.wrapper_pid,
        "child_pid": snapshot.child_pid,
        "child_start_ticks": snapshot.child_start_ticks,
    }
    receipt_id = hashlib.sha256(
        _canonical_bytes(
            {
                "snapshot_identity": stable_identity,
                "phase": receipt.phase,
                "state": receipt.state,
                "stage": stage,
                "mode": mode,
                "outcome": outcome,
                "retry_disposition": retry_disposition,
                "config_sha256": config_hash,
            }
        )
    ).hexdigest()
    return {
        "version": 1,
        "receipt_id": receipt_id,
        "recorded_at": recorded_at.astimezone(timezone.utc).isoformat(),
        "stage": stage,
        "mode": mode,
        "outcome": outcome,
        "retry_disposition": retry_disposition,
        "config": config,
        "config_sha256": config_hash,
        "snapshot_identity": stable_identity,
        "lease": asdict(receipt),
    }
