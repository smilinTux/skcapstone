"""Durable, fail-closed execution controls for the Atlas operator loop."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def action_fingerprint(proposal: dict) -> str:
    """Return a stable identity for one condition-to-action intent."""
    identity = {
        "app": proposal.get("app"),
        "condition": proposal.get("condition"),
        "object": proposal.get("object"),
        "action": proposal.get("action"),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class ExecutionState:
    """Atomic JSON state for cooldown, retry budget, and circuit breakers."""

    def __init__(
        self,
        root: str | Path,
        *,
        cooldown_seconds: float = 900.0,
        retry_budget: int = 3,
    ) -> None:
        self.root = Path(root)
        self.path = self.root / "execution-state.json"
        self.lock_path = self.root / "operator.lock"
        self.cooldown_seconds = cooldown_seconds
        self.retry_budget = retry_budget

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "actions": {}}
        try:
            payload = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"unreadable Atlas execution state: {exc}") from exc
        if payload.get("version") != 1 or not isinstance(payload.get("actions"), dict):
            raise RuntimeError("unsupported Atlas execution state")
        return payload

    def _save(self, payload: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(f".tmp-{os.getpid()}")
        data = json.dumps(payload, sort_keys=True, indent=2) + "\n"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                tmp.unlink()

    def eligibility(self, fingerprint: str, now: float) -> tuple[bool, str | None]:
        """Check cooldown and open-circuit state without modifying it."""
        entry = self._load()["actions"].get(fingerprint, {})
        if entry.get("circuit_open"):
            return False, "circuit-open"
        last = entry.get("last_attempt")
        if isinstance(last, (int, float)) and now - last < self.cooldown_seconds:
            return False, "cooldown"
        return True, None

    def record(self, fingerprint: str, now: float, *, success: bool, reason: str = "") -> None:
        """Record an attempt and open the circuit after the retry budget."""
        payload = self._load()
        entry = payload["actions"].setdefault(fingerprint, {})
        failures = 0 if success else int(entry.get("consecutive_failures", 0)) + 1
        entry.update(
            {
                "last_attempt": now,
                "last_success": success,
                "last_reason": reason,
                "consecutive_failures": failures,
                "circuit_open": failures >= self.retry_budget,
            }
        )
        self._save(payload)

    @contextmanager
    def single_flight(self) -> Iterator[None]:
        """Acquire the process-wide Atlas run lock without waiting."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("another Atlas operator pass is active") from exc
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def monotonic_deadline(max_runtime_seconds: float) -> float:
    """Return the monotonic deadline for a bounded pass."""
    if max_runtime_seconds <= 0:
        raise ValueError("max_runtime_seconds must be positive")
    return time.monotonic() + max_runtime_seconds


def assert_before_deadline(deadline: float) -> None:
    """Fail closed once a pass has exhausted its runtime budget."""
    if time.monotonic() >= deadline:
        raise TimeoutError("Atlas operator pass exceeded its maximum runtime")
