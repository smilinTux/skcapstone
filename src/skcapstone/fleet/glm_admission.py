"""Fail-closed, no-action GLM wave admission candidate.

This module only records a reservation. It has no launch, inference, HTTP, or
provider client. A separate, reviewed cutover would have to consume a live
reservation. Until then, host selectors must remain incapable of GLM dispatch.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, NoReturn, Sequence

AUTHORITY_HOST = "chiap08"
WORKER_HOSTS = ("chiap01", "chiap02", "chiap03")
SCHEMA = "skcapstone.glm-wave-ledger.v1"
SAMPLE_SEPARATION_SECONDS = 5
MAX_OBSERVATION_AGE_SECONDS = 10
MAX_LEDGER_AGE_SECONDS = 30


class AdmissionDenied(RuntimeError):  # noqa: N818
    """Raised whenever admission cannot be proved safe."""


@dataclass(frozen=True)
class Hold:
    """Exact hold state observed by the authority."""

    generation: str
    sha256: str
    active: bool


@dataclass(frozen=True)
class HostReport:
    """Read-only host session report."""

    host: str
    reachable: bool
    glm_auto_sessions: int
    observed_at: str


@dataclass(frozen=True)
class QueueSample:
    """Read-only zai queue observation."""

    observed_at: str
    active: int
    queued: int


@dataclass(frozen=True)
class WorkerBinding:
    """Identity and custody bound to one reserved worker."""

    host: str
    card_id: str
    agent_id: str
    session_id: str
    claim_id: str
    workspace: str


@dataclass(frozen=True)
class AdmissionSnapshot:
    """All frozen read-only inputs used for one decision."""

    hold: Hold
    hosts: tuple[HostReport, ...]
    queue_samples: tuple[QueueSample, QueueSample]


CrashHook = Callable[[Literal["before_rename", "after_rename"]], None]
SnapshotReader = Callable[[], AdmissionSnapshot]


def _deny(reason: str) -> NoReturn:
    """Fail closed with a stable reason."""

    raise AdmissionDenied(reason)


def _parse_time(value: object, field: str) -> datetime:
    """Parse a UTC timestamp or deny."""

    if not isinstance(value, str):
        _deny(f"invalid {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _deny(f"invalid {field}")
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _deny(f"invalid {field}")
    return parsed


def _validate_sha256(value: object, field: str) -> None:
    """Require a lowercase SHA-256 value."""

    if not isinstance(value, str) or len(value) != 64:
        _deny(f"invalid {field}")
    try:
        int(value, 16)
    except ValueError:
        _deny(f"invalid {field}")
    if value != value.lower():
        _deny(f"invalid {field}")


def _validate_bindings(bindings: Sequence[WorkerBinding]) -> None:
    """Require one complete, distinct 3-by-3 wave."""

    if len(bindings) != 9:
        _deny("wave must contain exactly nine workers")
    counts = Counter(binding.host for binding in bindings)
    if counts != Counter({host: 3 for host in WORKER_HOSTS}):
        _deny("wave must contain exactly three workers per approved host")
    fields = ("card_id", "agent_id", "session_id", "claim_id", "workspace")
    for field in fields:
        values = [getattr(binding, field) for binding in bindings]
        if any(not isinstance(value, str) or not value for value in values):
            _deny(f"empty worker {field}")
        if len(set(values)) != 9:
            _deny(f"conflicting worker {field}")
    for binding in bindings:
        if not Path(binding.workspace).is_absolute():
            _deny("workspace must be absolute")
        if binding.host not in binding.agent_id:
            _deny("agent identity is not host-distinct")


def _validate_snapshot(snapshot: AdmissionSnapshot, now: datetime) -> None:
    """Validate hold, host barrier, and two fresh queue samples."""

    _validate_sha256(snapshot.hold.sha256, "hold hash")
    if not snapshot.hold.generation:
        _deny("missing hold generation")
    if snapshot.hold.active:
        _deny("hold is active")

    if len(snapshot.hosts) != 3 or {report.host for report in snapshot.hosts} != set(WORKER_HOSTS):
        _deny("partial or conflicting host reports")
    for report in snapshot.hosts:
        observed = _parse_time(report.observed_at, "host observation time")
        age = (now - observed).total_seconds()
        if not report.reachable:
            _deny("host unreachable")
        if report.glm_auto_sessions != 0:
            _deny("stale or live glm-auto session")
        if age < 0 or age > MAX_OBSERVATION_AGE_SECONDS:
            _deny("stale host report")

    first, second = snapshot.queue_samples
    first_at = _parse_time(first.observed_at, "queue sample time")
    second_at = _parse_time(second.observed_at, "queue sample time")
    if (second_at - first_at).total_seconds() != SAMPLE_SEPARATION_SECONDS:
        _deny("queue samples are not five seconds apart")
    age = (now - second_at).total_seconds()
    if age < 0 or age > MAX_OBSERVATION_AGE_SECONDS:
        _deny("stale in-flight request samples")
    if any(sample.active != 0 or sample.queued != 0 for sample in (first, second)):
        _deny("zai queue is not idle")


def _load_ledger(path: Path, now: datetime) -> dict[str, object]:
    """Load and strictly validate an existing generation ledger."""

    try:
        raw = path.read_text(encoding="utf-8")
        ledger = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        _deny("missing or malformed ledger")
    if not isinstance(ledger, dict) or set(ledger) != {
        "schema",
        "authority_host",
        "generation",
        "status",
        "updated_at",
        "hold",
        "workers",
    }:
        _deny("invalid ledger shape")
    if ledger["schema"] != SCHEMA or ledger["authority_host"] != AUTHORITY_HOST:
        _deny("conflicting ledger authority")
    generation = ledger["generation"]
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        _deny("non-monotonic ledger generation")
    if ledger["status"] not in ("complete", "live"):
        _deny("invalid ledger status")
    updated = _parse_time(ledger["updated_at"], "ledger update time")
    age = (now - updated).total_seconds()
    if age < 0 or age > MAX_LEDGER_AGE_SECONDS:
        _deny("stale ledger")
    hold = ledger["hold"]
    workers = ledger["workers"]
    if not isinstance(hold, dict) or set(hold) != {"generation", "sha256"}:
        _deny("invalid ledger hold binding")
    _validate_sha256(hold["sha256"], "ledger hold hash")
    if not isinstance(hold["generation"], str) or not hold["generation"]:
        _deny("invalid ledger hold generation")
    if not isinstance(workers, list):
        _deny("invalid ledger workers")
    if ledger["status"] == "live":
        try:
            _validate_bindings([WorkerBinding(**item) for item in workers])
        except (TypeError, AttributeError):
            _deny("invalid live worker bindings")
    elif workers:
        _deny("complete ledger retains workers")
    return ledger


def _atomic_replace(path: Path, value: dict[str, object], crash_hook: CrashHook | None) -> None:
    """Serialize, temp-write, fsync, replace, and fsync the parent directory."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if crash_hook:
            crash_hook("before_rename")
        os.replace(temporary, path)
        temporary = ""
        if crash_hook:
            crash_hook("after_rename")
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def admit_wave(
    *,
    authority_host: str,
    ledger_path: Path,
    lock_path: Path,
    proposed_generation: int,
    bindings: Sequence[WorkerBinding],
    snapshot_reader: SnapshotReader,
    now: datetime,
    crash_hook: CrashHook | None = None,
) -> dict[str, object]:
    """Atomically reserve one entire wave, or deny without any dispatch action."""

    if authority_host != AUTHORITY_HOST:
        _deny("only chiap08 may write or dispatch")
    if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
        _deny("now must be UTC")
    _validate_bindings(bindings)
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        ledger = _load_ledger(ledger_path, now)
        if ledger["status"] == "live":
            _deny("live generation is never refilled")
        if proposed_generation != ledger["generation"] + 1:
            _deny("non-monotonic proposed generation")

        first = snapshot_reader()
        _validate_snapshot(first, now)
        second = snapshot_reader()
        _validate_snapshot(second, now)
        if second.hold != first.hold:
            _deny("hold changed during admission")
        if second.hosts != first.hosts or second.queue_samples != first.queue_samples:
            _deny("conflicting admission snapshot")

        replacement: dict[str, object] = {
            "schema": SCHEMA,
            "authority_host": AUTHORITY_HOST,
            "generation": proposed_generation,
            "status": "live",
            "updated_at": now.isoformat().replace("+00:00", "Z"),
            "hold": {
                "generation": first.hold.generation,
                "sha256": first.hold.sha256,
            },
            "workers": [asdict(binding) for binding in bindings],
        }
        _atomic_replace(ledger_path, replacement, crash_hook)
        return replacement
