"""Bounded execution and durable, immutable receipts for metadata handoffs.

An uncertain operation is never retried. Timeout returns to the caller while
retaining occupancy until the worker exits. A late result cannot replace the
terminal receipt. Authority systems still own claims, reviews, and permission.
"""

from __future__ import annotations

import hashlib
import json
import math
import queue
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .skrsi_registry import SKRSIError, canonical_json

_SQLITE_BUSY_TIMEOUT_MS = 2_000


@dataclass(frozen=True)
class HandoffContract:
    """One owner per role and finite limits for a boundary."""

    producer: str
    consumer: str
    natural_key: str
    queue_bound: int
    timeout_seconds: float
    retry_attempts: int
    backoff_seconds: float
    terminal_evidence: str
    recovery_owner: str
    escalation_recipient: str
    notification_only: bool = True

    def __post_init__(self) -> None:
        for name in (
            "producer",
            "consumer",
            "natural_key",
            "terminal_evidence",
            "recovery_owner",
            "escalation_recipient",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or "," in value:
                raise ValueError("exactly one nonempty handoff owner or identifier is required")
        if type(self.queue_bound) is not int or not 1 <= self.queue_bound <= 10000:
            raise ValueError("invalid queue bound")
        if type(self.retry_attempts) is not int or not 0 <= self.retry_attempts <= 3:
            raise ValueError("invalid retry bound")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("invalid timeout")
        if not math.isfinite(self.backoff_seconds) or self.backoff_seconds < 0:
            raise ValueError("invalid backoff")
        if self.notification_only is not True:
            raise ValueError("escalation must be notification-only")


def _contract(producer: str, consumer: str, owner: str, key: str = "input_sha256"):
    return HandoffContract(
        producer, consumer, key, 16, 2.0, 2, 0.05, "sha256:terminal-receipt", owner, "mero"
    )


FIRST_WAVE_HANDOFFS = {
    "cardstore-to-skrsi": _contract("CardStore", "SKRSI", "atlas", "card_event_id"),
    "fleet-to-skrsi": _contract("SKFleet", "SKRSI", "niobe", "fleet_event_id"),
    "mail-to-skrsi": _contract("SKMail", "SKRSI", "mero", "envelope_id"),
    "registry-to-collector": _contract("registry", "collector", "atlas", "target_revision"),
    "collector-to-evaluator": _contract("collector", "evaluator", "atlas", "cohort_hash"),
    "evaluator-to-controller": _contract("evaluator", "controller", "atlas", "evaluation_hash"),
    "controller-to-dashboard": _contract("controller", "dashboard-projection", "tank"),
    "dashboard-query-delivery": _contract("dashboard-projection", "query-delivery", "tank"),
    "canary-to-review": _contract("canary", "Link", "link", "evaluation_hash"),
    "evidence-to-review": _contract("Link", "Seraph", "link", "source_card+head_revision"),
}

_SQLITE_LOCK_TIMEOUT_SECONDS = 5.0


class HandoffError(SKRSIError):
    """A terminal or admission failure; receipt is available through read()."""


class RetryBeforeEffectError(Exception):
    """Consumer explicitly certifies that no effect occurred; retry is safe."""


class HandoffRuntime:
    """Host-local durable single-flight execution shared by runtime instances.

    All instances for one authority must use the same database. SQLite fences
    processes on that host; it is not a distributed lock across Syncthing hosts.
    Native CardStore locking remains responsible for distributed card claims.
    """

    def __init__(self, path: Path, *, contracts=None):
        self.path = Path(path)
        self.contracts = dict(FIRST_WAVE_HANDOFFS if contracts is None else contracts)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS handoffs (
                boundary TEXT, natural_key TEXT, fingerprint TEXT, contract_hash TEXT,
                active INTEGER, receipt TEXT, result TEXT,
                PRIMARY KEY(boundary, natural_key))""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS immutable_terminal
                BEFORE UPDATE OF receipt ON handoffs
                WHEN OLD.receipt IS NOT NULL AND NEW.receipt IS NOT OLD.receipt
                BEGIN SELECT RAISE(ABORT, 'immutable terminal receipt'); END""")
        self._queue: queue.Queue = queue.Queue(maxsize=16)
        # Daemon workers retain bounded occupancy when a consumer never returns.
        for _ in range(4):
            threading.Thread(target=self._worker, daemon=True).start()

    @contextmanager
    def _db(self):
        # SQLite's native busy handler retries only lock contention. Keep the
        # wait bounded below the handoff deadline while allowing concurrent
        # source-head replays to serialize at the shared transaction boundary.
        db = sqlite3.connect(self.path, timeout=_SQLITE_BUSY_TIMEOUT_MS / 1_000)
        try:
            db.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
            with db:
                yield db
        finally:
            db.close()

    def read(self, boundary: str, key: str) -> dict[str, Any] | None:
        """Read terminal evidence without executing or mutating the handoff."""
        with self._db() as db:
            row = db.execute(
                "SELECT receipt FROM handoffs WHERE boundary=? AND natural_key=?", (boundary, key)
            ).fetchone()
        if not row or row[0] is None:
            return None
        receipt = json.loads(row[0])
        recorded_hash = receipt.pop("sha256")
        if hashlib.sha256(canonical_json(receipt)).hexdigest() != recorded_hash:
            raise HandoffError("terminal evidence hash mismatch")
        return {**receipt, "sha256": recorded_hash}

    def _finish(self, boundary, key, policy, status, attempts, result=None):
        with self._db() as db:
            fingerprint = db.execute(
                "SELECT fingerprint FROM handoffs WHERE boundary=? AND natural_key=?",
                (boundary, key),
            ).fetchone()[0]
        receipt = {
            "schema": "skrsi.handoff-terminal.v1",
            "boundary": boundary,
            "natural_key_hash": hashlib.sha256(key.encode()).hexdigest(),
            "input_and_authority_hash": fingerprint,
            "contract": asdict(policy),
            "status": status,
            "attempts": attempts,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "notification": {
                "recipient": policy.escalation_recipient,
                "mode": "notification-only",
                "required": status != "success",
            },
            "recovery_owner": policy.recovery_owner,
            "result_hash": hashlib.sha256(canonical_json(result)).hexdigest(),
        }
        receipt["sha256"] = hashlib.sha256(canonical_json(receipt)).hexdigest()
        with self._db() as db:
            db.execute(
                """UPDATE handoffs SET receipt=?, result=?
                       WHERE boundary=? AND natural_key=? AND receipt IS NULL""",
                (canonical_json(receipt).decode(), canonical_json(result).decode(), boundary, key),
            )

    def notifications(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Read bounded durable escalation messages for the owning service's mailbox."""
        if not 1 <= limit <= 100:
            raise ValueError("notification read bound must be 1 through 100")
        with self._db() as db:
            rows = db.execute(
                "SELECT boundary,natural_key FROM handoffs WHERE receipt IS NOT NULL "
                "AND json_extract(receipt,'$.status') != 'success' ORDER BY rowid LIMIT ?",
                (limit,),
            ).fetchall()
        return [self.read(boundary, key) for boundary, key in rows]

    def execute(
        self,
        boundary: str,
        key: str,
        fingerprint: str,
        operation: Callable,
        *,
        authorize: Callable[[], bool],
        quality: Callable[[], bool],
        authority: Callable[[], str],
        expected_revision: str,
    ) -> Any:
        """Execute metadata work with live gates, one key, and a terminal deadline.

        Gate callbacks are trusted authority adapters, never user-supplied flags.
        They are checked in the worker before each attempt and before publishing
        success. No exception except RetryBeforeEffectError triggers a retry.
        """
        policy = self.contracts[boundary]
        if not key or len(key) > 512 or not expected_revision:
            raise HandoffError("invalid handoff key or authority revision")
        if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise HandoffError("invalid input fingerprint")
        fingerprint = hashlib.sha256(
            canonical_json({"input": fingerprint, "authority_revision": expected_revision})
        ).hexdigest()
        contract_hash = hashlib.sha256(canonical_json(asdict(policy))).hexdigest()
        deadline = time.monotonic() + policy.timeout_seconds
        admitted = False
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT fingerprint,contract_hash,receipt,result FROM handoffs "
                "WHERE boundary=? AND natural_key=?",
                (boundary, key),
            ).fetchone()
            if prior:
                if prior[:2] != (fingerprint, contract_hash):
                    raise HandoffError("replay identity or contract collision")
            else:
                occupied = db.execute(
                    "SELECT COUNT(*) FROM handoffs WHERE boundary=? AND active=1", (boundary,)
                ).fetchone()[0]
                db.execute(
                    "INSERT INTO handoffs VALUES (?,?,?,?,?,NULL,NULL)",
                    (
                        boundary,
                        key,
                        fingerprint,
                        contract_hash,
                        int(occupied < policy.queue_bound),
                    ),
                )
                admitted = occupied < policy.queue_bound
        if not prior and not admitted:
            self._finish(boundary, key, policy, "saturated", 0)
        elif admitted:
            job = (
                boundary,
                key,
                policy,
                deadline,
                operation,
                authorize,
                quality,
                authority,
                expected_revision,
            )
            try:
                self._queue.put_nowait(job)
            except queue.Full:
                self._finish(boundary, key, policy, "saturated", 0)
                self._release(boundary, key)
        while time.monotonic() < deadline:
            receipt = self.read(boundary, key)
            if receipt is not None:
                if receipt["status"] != "success":
                    raise HandoffError(receipt["status"])
                # Replay is data, not a new authorization. Validate it in a bounded worker.
                if prior:
                    return self._replay_result(
                        boundary, key, authorize, quality, authority, expected_revision, deadline
                    )
                with self._db() as db:
                    value = db.execute(
                        "SELECT result FROM handoffs WHERE boundary=? AND natural_key=?",
                        (boundary, key),
                    ).fetchone()[0]
                if (
                    hashlib.sha256(canonical_json(json.loads(value))).hexdigest()
                    != receipt["result_hash"]
                ):
                    raise HandoffError("result hash mismatch")
                return json.loads(value)
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        self._finish(boundary, key, policy, "timeout-uncertain", None)
        raise HandoffError("timeout-uncertain")

    def _replay_result(self, boundary, key, authorize, quality, authority, revision, deadline):
        # A replay is never allowed to mint fresh authority. Gate callbacks must
        # be cheap, but run with the same worker and deadline as new work.
        done = threading.Event()
        holder = {}

        def check():
            self._gates(authorize, quality, authority, revision)
            with self._db() as db:
                value = db.execute(
                    "SELECT result FROM handoffs WHERE boundary=? AND natural_key=?",
                    (boundary, key),
                ).fetchone()[0]
            result = json.loads(value)
            receipt = self.read(boundary, key)
            if hashlib.sha256(canonical_json(result)).hexdigest() != receipt["result_hash"]:
                raise HandoffError("result hash mismatch")
            return result

        try:
            self._queue.put_nowait((check, done, holder))
        except queue.Full as exc:
            raise HandoffError("replay saturated") from exc
        if not done.wait(max(0, deadline - time.monotonic())):
            raise HandoffError("replay timeout")
        if "error" in holder:
            raise HandoffError(holder["error"])
        return holder["result"]

    @staticmethod
    def _gates(authorize, quality, authority, revision):
        if authorize() is not True:
            raise HandoffError("authorization-denied")
        if quality() is not True:
            raise HandoffError("quality-denied")
        if authority() != revision:
            raise HandoffError("stale-authority")

    def _release(self, boundary, key):
        with self._db() as db:
            db.execute(
                "UPDATE handoffs SET active=0 WHERE boundary=? AND natural_key=?", (boundary, key)
            )

    def _worker(self):
        while True:
            job = self._queue.get()
            if len(job) == 3:
                check, done, holder = job
                try:
                    holder["result"] = check()
                except Exception as exc:
                    holder["error"] = type(exc).__name__
                finally:
                    done.set()
                continue
            boundary, key, policy, deadline, operation, authorize, quality, authority, revision = (
                job
            )
            attempts = 0
            try:
                for attempt in range(policy.retry_attempts + 1):
                    if time.monotonic() >= deadline or self.read(boundary, key) is not None:
                        break
                    self._gates(authorize, quality, authority, revision)
                    if time.monotonic() >= deadline:
                        break
                    attempts += 1
                    try:
                        result = operation()
                    except RetryBeforeEffectError:
                        if attempt == policy.retry_attempts:
                            self._finish(boundary, key, policy, "retry-exhausted", attempts)
                            break
                        delay = policy.backoff_seconds * 2**attempt
                        time.sleep(min(delay, max(0, deadline - time.monotonic())))
                        continue
                    self._gates(authorize, quality, authority, revision)
                    if time.monotonic() < deadline:
                        self._finish(boundary, key, policy, "success", attempts, result)
                    break
            except Exception as exc:
                allowed = {"authorization-denied", "quality-denied", "stale-authority"}
                status = (
                    str(exc)
                    if isinstance(exc, HandoffError) and str(exc) in allowed
                    else type(exc).__name__
                )
                self._finish(boundary, key, policy, status, attempts)
            finally:
                self._finish(boundary, key, policy, "timeout-uncertain", attempts)
                self._release(boundary, key)
