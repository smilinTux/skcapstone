"""Nonblocking, generation-fenced guards for recurring Link and Mero cycles.

The lock is advisory and host-local by design: recurring invocations for a seat
must use the same state directory.  Receipts are immutable JSONL events.  A
quiet process is never treated as stale.  Linux boot ID and ``/proc`` process
start ticks, rather than age or output, establish whether an unfinished prior
generation was abandoned.
"""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Generic, Literal, TypeVar

Seat = Literal["link", "mero"]
T = TypeVar("T")
_ALLOWED_SEATS = frozenset({"link", "mero"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _boot_id(proc_root: Path) -> str:
    return (proc_root / "sys/kernel/random/boot_id").read_text(encoding="ascii").strip()


def _process_start_ticks(proc_root: Path, pid: int) -> int:
    # The comm field is parenthesized and may contain spaces or parentheses.
    stat = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
    end = stat.rfind(")")
    if end < 0:
        raise ValueError("malformed process stat")
    fields_after_comm = stat[end + 2 :].split()
    return int(fields_after_comm[19])  # field 22 overall


def _serialized_line(event: dict[str, object]) -> str:
    line = json.dumps(event, sort_keys=True, separators=(",", ":"))
    parsed = json.loads(line)
    if parsed != event or not isinstance(parsed, dict):
        raise ValueError("receipt did not round-trip through JSON")
    return line + "\n"


@dataclass(frozen=True)
class ProcessGeneration:
    """Exact Linux process generation evidence for a cycle owner."""

    pid: int
    boot_id: str
    start_ticks: int

    @classmethod
    def current(cls, proc_root: Path = Path("/proc")) -> "ProcessGeneration":
        pid = os.getpid()
        return cls(pid, _boot_id(proc_root), _process_start_ticks(proc_root, pid))

    def is_live(self, proc_root: Path = Path("/proc")) -> bool:
        """Return true only when PID, boot generation, and start time all match."""
        try:
            return (
                _boot_id(proc_root) == self.boot_id
                and _process_start_ticks(proc_root, self.pid) == self.start_ticks
            )
        except (OSError, ValueError):
            return False


@dataclass(frozen=True)
class CycleResult(Generic[T]):
    """Result of attempting one recurring cycle."""

    cycle_id: str
    ran: bool
    value: T | None = None
    live_cycle_id: str | None = None


class SeatCycleGuard:
    """Serialize one recurring seat without waiting for an overlapping run."""

    def __init__(
        self,
        state_dir: Path,
        seat: Seat,
        *,
        proc_root: Path = Path("/proc"),
        clock: Callable[[], str] = _utc_now,
        id_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        normalized = seat.lower()
        if normalized not in _ALLOWED_SEATS:
            raise ValueError(f"unsupported recurring seat: {seat}")
        self.state_dir = Path(state_dir)
        self.seat: Seat = normalized  # type: ignore[assignment]
        self.proc_root = proc_root
        self.clock = clock
        self.id_factory = id_factory
        self.lock_path = self.state_dir / f"{normalized}.cycle.lock"
        self.receipt_path = self.state_dir / f"{normalized}.cycle.receipts.jsonl"

    def run(self, operation: Callable[[str], T]) -> CycleResult[T]:
        """Run once, or record an honest no-op when this seat is already live."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        cycle_id = str(self.id_factory())
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                owner = self._read_lock_owner(lock_file)
                owner_cycle_id = owner.get("cycle_id")
                live_id = (
                    owner_cycle_id
                    if self._owner_is_exactly_live(owner) and isinstance(owner_cycle_id, str)
                    else None
                )
                self._append_receipt(
                    {
                        "event": "overlap_noop",
                        "cycle_id": cycle_id,
                        "seat": self.seat,
                        "at": self.clock(),
                        "live_cycle_id": live_id,
                        "owner_evidence_verified": live_id is not None,
                    }
                )
                return CycleResult(cycle_id=cycle_id, ran=False, live_cycle_id=live_id)

            try:
                process = ProcessGeneration.current(self.proc_root)
                prior = self._latest_unfinished_start()
                if prior is not None and not self._receipt_owner_is_live(prior):
                    self._append_receipt(
                        {
                            "event": "abandoned",
                            "cycle_id": prior["cycle_id"],
                            "seat": self.seat,
                            "at": self.clock(),
                            "observed_by_cycle_id": cycle_id,
                            "process_generation": prior["process_generation"],
                            "reason": "exact_process_generation_not_live",
                        }
                    )

                owner = {
                    "cycle_id": cycle_id,
                    "seat": self.seat,
                    "process_generation": {
                        "pid": process.pid,
                        "boot_id": process.boot_id,
                        "start_ticks": process.start_ticks,
                    },
                }
                self._write_lock_owner(lock_file, owner)
                self._append_receipt({"event": "start", "at": self.clock(), **owner})
                try:
                    value = operation(cycle_id)
                except BaseException:
                    # No finish event is truthful.  A later generation will append the
                    # abandoned receipt after proving this process generation is gone.
                    raise
                self._append_receipt(
                    {
                        "event": "finish",
                        "cycle_id": cycle_id,
                        "seat": self.seat,
                        "at": self.clock(),
                    }
                )
                return CycleResult(cycle_id=cycle_id, ran=True, value=value)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_lock_owner(self, lock_file: object) -> dict[str, object]:
        try:
            lock_file.seek(0)  # type: ignore[attr-defined]
            value = json.loads(lock_file.read())  # type: ignore[attr-defined]
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _write_lock_owner(self, lock_file: object, owner: dict[str, object]) -> None:
        encoded = _serialized_line(owner).rstrip("\n")
        lock_file.seek(0)  # type: ignore[attr-defined]
        lock_file.truncate()  # type: ignore[attr-defined]
        lock_file.write(encoded)  # type: ignore[attr-defined]
        lock_file.flush()  # type: ignore[attr-defined]
        os.fsync(lock_file.fileno())  # type: ignore[attr-defined]

    def _append_receipt(self, event: dict[str, object]) -> None:
        line = _serialized_line(event)
        with self.receipt_path.open("a+", encoding="utf-8") as receipt_file:
            fcntl.flock(receipt_file.fileno(), fcntl.LOCK_EX)
            try:
                receipt_file.seek(0)
                for existing in receipt_file:
                    parsed = json.loads(existing)
                    if not isinstance(parsed, dict):
                        raise ValueError("receipt line is not a JSON object")
                receipt_file.seek(0, os.SEEK_END)
                receipt_file.write(line)
                receipt_file.flush()
                os.fsync(receipt_file.fileno())
            finally:
                fcntl.flock(receipt_file.fileno(), fcntl.LOCK_UN)

    def _receipts(self) -> list[dict[str, object]]:
        if not self.receipt_path.exists():
            return []
        events: list[dict[str, object]] = []
        with self.receipt_path.open(encoding="utf-8") as receipt_file:
            for line in receipt_file:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("receipt line is not a JSON object")
                events.append(value)
        return events

    def _latest_unfinished_start(self) -> dict[str, object] | None:
        events = self._receipts()
        finished = {
            event.get("cycle_id")
            for event in events
            if event.get("event") in {"finish", "abandoned"}
        }
        for event in reversed(events):
            if event.get("event") == "start" and event.get("cycle_id") not in finished:
                return event
        return None

    def _receipt_owner_is_live(self, receipt: dict[str, object]) -> bool:
        return self._generation_is_live(receipt.get("process_generation"))

    def _owner_is_exactly_live(self, owner: dict[str, object]) -> bool:
        return (
            owner.get("seat") == self.seat
            and isinstance(owner.get("cycle_id"), str)
            and self._generation_is_live(owner.get("process_generation"))
        )

    def _generation_is_live(self, raw: object) -> bool:
        if not isinstance(raw, dict):
            return False
        try:
            generation = ProcessGeneration(
                pid=int(raw["pid"]),
                boot_id=str(raw["boot_id"]),
                start_ticks=int(raw["start_ticks"]),
            )
        except (KeyError, TypeError, ValueError):
            return False
        return generation.is_live(self.proc_root)
