"""Fail-closed, no-action GLM wave admission candidate.

This module only records a reservation. It has no launch, inference, HTTP, or
provider client. A separate, reviewed cutover would have to consume a live
reservation. Until then, host selectors must remain incapable of GLM dispatch.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import stat
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, NoReturn, Sequence

AUTHORITY_HOST = "chiap08"
AUTHORITY_DIRECTORY = Path("/var/lib/skcapstone-local/glm-admission")
LOCK_PATH = AUTHORITY_DIRECTORY / "admission.lock"
LEDGER_PATH = AUTHORITY_DIRECTORY / "generation.json"
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
class QueueSample:
    """Read-only zai queue observation."""

    observed_at: str
    active: int
    queued: int


@dataclass(frozen=True)
class HostReport:
    """Authoritative read-only host pressure and session report."""

    host: str
    reachable: bool
    glm_auto_sessions: int
    observed_at: str
    http_429: bool
    queue_samples: tuple[QueueSample, QueueSample]


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
        if report.http_429:
            _deny("authoritative host reported 429")
        first, second = report.queue_samples
        first_at = _parse_time(first.observed_at, "queue sample time")
        second_at = _parse_time(second.observed_at, "queue sample time")
        if (second_at - first_at).total_seconds() != SAMPLE_SEPARATION_SECONDS:
            _deny("queue samples are not five seconds apart")
        sample_age = (now - second_at).total_seconds()
        if sample_age < 0 or sample_age > MAX_OBSERVATION_AGE_SECONDS:
            _deny("stale in-flight request samples")
        if first.queued > 0 and second.queued > 0:
            _deny("positive queue persisted for two samples")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for sample in (first, second)
            for value in (sample.active, sample.queued)
        ):
            _deny("invalid queue pressure")


def _physical_hostname() -> str:
    """Return the normalized hostname reported by the operating system."""

    try:
        hostname = socket.gethostname()
    except OSError:
        _deny("physical hostname unavailable")
    if not isinstance(hostname, str):
        _deny("physical hostname unavailable")
    return hostname.lower().rstrip(".")


def _safe_authority_directory() -> int:
    """Open the fixed local directory after strict ownership and type checks."""

    try:
        lexical = Path(os.path.abspath(AUTHORITY_DIRECTORY))
        if AUTHORITY_DIRECTORY != lexical or AUTHORITY_DIRECTORY.resolve(strict=True) != lexical:
            _deny("unsafe authority directory")
        metadata = AUTHORITY_DIRECTORY.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            _deny("unsafe authority directory")
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            _deny("unsafe authority directory ownership or mode")
        fd = os.open(
            AUTHORITY_DIRECTORY,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(fd)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            os.close(fd)
            _deny("unsafe authority directory")
        return fd
    except AdmissionDenied:
        raise
    except OSError:
        _deny("unsafe authority directory")


def _validate_regular_file(fd: int, name: str) -> os.stat_result:
    """Require one owner-only, non-symlink regular file."""

    metadata = os.fstat(fd)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        _deny(f"unsafe {name} file")
    return metadata


def _open_lock(directory_fd: int) -> int:
    """Open or create the fixed lock without following a symlink."""

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(LOCK_PATH.name, flags, 0o600, dir_fd=directory_fd)
        _validate_regular_file(fd, "lock")
        return fd
    except AdmissionDenied:
        try:
            os.close(fd)
        except (NameError, OSError):
            pass
        raise
    except OSError:
        _deny("unsafe lock file")


def _load_ledger(directory_fd: int, now: datetime) -> tuple[dict[str, object], os.stat_result]:
    """Load and strictly validate the fixed generation ledger."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(LEDGER_PATH.name, flags, dir_fd=directory_fd)
        metadata = _validate_regular_file(fd, "ledger")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            ledger = json.load(stream)
    except AdmissionDenied:
        if "fd" in locals() and fd >= 0:
            os.close(fd)
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        if "fd" in locals() and fd >= 0:
            os.close(fd)
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
    return ledger, metadata


def _atomic_replace(
    directory_fd: int,
    previous: os.stat_result,
    value: dict[str, object],
    crash_hook: CrashHook | None,
) -> None:
    """Safely replace the fixed ledger and fsync its local directory."""

    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{LEDGER_PATH.name}.", dir=AUTHORITY_DIRECTORY)
    temporary_name = Path(temporary).name
    try:
        os.fchmod(fd, 0o600)
        _validate_regular_file(fd, "temporary ledger")
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        current = os.stat(LEDGER_PATH.name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (
            previous.st_dev,
            previous.st_ino,
        ):
            _deny("ledger changed before atomic replace")
        if crash_hook:
            crash_hook("before_rename")
        os.replace(
            temporary_name,
            LEDGER_PATH.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_name = ""
        if crash_hook:
            crash_hook("after_rename")
        os.fsync(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def _admit_wave(
    *,
    bindings: Sequence[WorkerBinding],
    snapshot_reader: SnapshotReader,
    now: datetime,
    crash_hook: CrashHook | None = None,
) -> dict[str, object]:
    """Atomically reserve one entire wave through the private tested core."""

    if _physical_hostname() != AUTHORITY_HOST:
        _deny("physical host is not chiap08")
    if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
        _deny("now must be UTC")
    _validate_bindings(bindings)
    directory_fd = _safe_authority_directory()
    try:
        lock_fd = _open_lock(directory_fd)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            ledger, previous = _load_ledger(directory_fd, now)
            if ledger["status"] == "live":
                _deny("live generation is never refilled")
            proposed_generation = ledger["generation"] + 1

            first = snapshot_reader()
            _validate_snapshot(first, now)
            second = snapshot_reader()
            _validate_snapshot(second, now)
            if second.hold != first.hold:
                _deny("hold changed during admission")
            if second.hosts != first.hosts:
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
            _atomic_replace(directory_fd, previous, replacement, crash_hook)
            return replacement
        finally:
            os.close(lock_fd)
    finally:
        os.close(directory_fd)


def _reserve_wave(
    bindings: Sequence[WorkerBinding], snapshot: AdmissionSnapshot, now: datetime
) -> dict[str, object]:
    """Reserve a wave from one already folded authoritative snapshot."""

    return _admit_wave(bindings=bindings, snapshot_reader=lambda: snapshot, now=now)


def _abort_wave(bindings: Sequence[WorkerBinding], now: datetime) -> dict[str, object]:
    """Atomically mark the exact live wave complete during launch rollback."""

    if _physical_hostname() != AUTHORITY_HOST:
        _deny("physical host is not chiap08")
    directory_fd = _safe_authority_directory()
    try:
        lock_fd = _open_lock(directory_fd)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            ledger, previous = _load_ledger(directory_fd, now)
            expected = [asdict(binding) for binding in bindings]
            if ledger["status"] != "live" or ledger["workers"] != expected:
                _deny("rollback wave does not match live reservation")
            replacement = {
                **ledger,
                "status": "complete",
                "updated_at": now.isoformat().replace("+00:00", "Z"),
                "workers": [],
            }
            _atomic_replace(directory_fd, previous, replacement, None)
            return replacement
        finally:
            os.close(lock_fd)
    finally:
        os.close(directory_fd)
