"""Disabled-by-default, simulation-only delivery for immutable reports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping

from .dashboard_reports import (
    ReportSnapshotError,
    ReportSnapshotStore,
    validate_report_snapshot,
)

DELIVERY_CAPABILITY = "skdashboard.reports.deliver.simulate"
_CLASSIFICATION_ORDER = ("public", "internal", "confidential", "restricted")
CLASSIFICATIONS = frozenset(_CLASSIFICATION_ORDER)
REDACTION_PROFILES = frozenset({"exact_snapshot", "metadata_only"})
MAX_ATTEMPTS = 3
MAX_BATCH = 100
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
CLAIM_SECONDS = 30
_ID = re.compile(r"^[a-z][a-z0-9._:-]{7,127}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,255}$")
_SENSITIVE = re.compile(r"(?:bearer|capability.?token|secret|password|credential|://)", re.I)
_CLASSIFICATION_RANK = {name: index for index, name in enumerate(_CLASSIFICATION_ORDER)}


class ReportDeliveryError(ValueError):
    """A delivery request cannot safely enter or advance the simulation."""


class ReportDeliveryDenied(ReportDeliveryError):
    """Exact delivery approval or policy is absent, denied, or expired."""


class ReportDeliveryConflict(ReportDeliveryError):
    """Immutable or terminal delivery state conflicts with the request."""


class SimulationDestinationError(RuntimeError):
    """A public-synthetic destination produced a retryable failure."""


@dataclass(frozen=True)
class DeliveryApproval:
    """Sanitized exact approval facts, never bearer or credential material."""

    report_hash: str
    destination_id: str
    audience: tuple[str, ...]
    classification: str
    source_rights_ref: str
    purpose: str
    retention_days: int
    redaction_profile: str
    policy_decision_ref: str
    approval_ref: str
    approved_at: str
    expires_at: str
    destination_verified_until: str
    capability: str = DELIVERY_CAPABILITY
    policy_state: str = "allow"
    approval_state: str = "approved"
    destination_state: str = "verified"
    source_rights_state: str = "approved"
    simulation_only: bool = True


def _canonical(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _time(value: str, field: str) -> datetime:
    if not isinstance(value, str):
        raise ReportDeliveryError(f"{field} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReportDeliveryError(f"{field} must be a timestamp") from error
    if parsed.tzinfo is None:
        raise ReportDeliveryError(f"{field} requires an explicit timezone")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe(value: str, field: str, *, identifier: bool = False) -> str:
    pattern = _ID if identifier else _REF
    if not isinstance(value, str) or not pattern.fullmatch(value) or _SENSITIVE.search(value):
        raise ReportDeliveryError(f"{field} is invalid or sensitive")
    return value


def _snapshot_classification(snapshot: Mapping[str, object]) -> str:
    levels = {
        metric.get("classification", {}).get("level")
        for section in snapshot.get("sections", [])
        if isinstance(section, dict)
        for metric in section.get("metric_results", [])
        if isinstance(metric, dict)
    }
    if not levels or not levels <= CLASSIFICATIONS:
        raise ReportDeliveryError("snapshot classification is unavailable")
    return max(levels, key=_CLASSIFICATION_RANK.__getitem__)


def _safe_snapshot(snapshot: Mapping[str, object]) -> dict:
    try:
        validated = validate_report_snapshot(snapshot)
    except ReportSnapshotError as error:
        raise ReportDeliveryDenied("immutable report snapshot is invalid or protected") from error
    scope = validated.get("scope", {})
    if any(key in scope for key in ("tenant_id", "matter_id")):
        raise ReportDeliveryDenied(
            "protected Tenant or Matter reporting requires the SKLegal external-action state machine"
        )
    return validated


class SimulationDestination:
    """Deterministic destination with idempotent, content-free receipts."""

    simulation_only = True

    def __init__(self, destination_id: str, outcomes: tuple[str, ...] = ("success",)):
        self.destination_id = _safe(destination_id, "destination_id", identifier=True)
        if not outcomes or any(value not in {"success", "error"} for value in outcomes):
            raise ReportDeliveryError("simulation outcomes are invalid")
        self._outcomes = list(outcomes)
        self._receipts: dict[str, dict] = {}
        self.calls = 0

    def send(
        self,
        *,
        idempotency_key: str,
        payload: bytes,
        metadata: Mapping[str, object],
        delivered_at: str,
    ) -> dict:
        if idempotency_key in self._receipts:
            return dict(self._receipts[idempotency_key])
        self.calls += 1
        outcome = self._outcomes.pop(0) if self._outcomes else "success"
        if outcome == "error":
            raise SimulationDestinationError("public synthetic destination error")
        receipt = {
            "receipt_id": "rcpt-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:24],
            "destination_id": self.destination_id,
            "delivery_hash": str(metadata["delivery_hash"]),
            "delivered_at": delivered_at,
            "simulation": True,
        }
        self._receipts[idempotency_key] = receipt
        return dict(receipt)


class ReportDeliveryService:
    """Transactional subscription, outbox, retry, receipt, and audit simulation."""

    def __init__(
        self,
        home: Path,
        *,
        policy_checker: Callable[[DeliveryApproval], bool],
        snapshot_store=None,
    ) -> None:
        if not callable(policy_checker):
            raise ReportDeliveryError("a policy checker is required")
        self.policy_checker = policy_checker
        self.snapshot_store = snapshot_store or ReportSnapshotStore(home)
        root = Path(home).expanduser() / "report-delivery"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink() or not root.is_dir() or stat.S_IMODE(root.stat().st_mode) & 0o077:
            raise ReportDeliveryError("delivery state directory is unsafe")
        self.database = root / "simulation.sqlite3"
        if self.database.exists():
            metadata = self.database.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise ReportDeliveryError("delivery database is unsafe")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS subscriptions (
                  subscription_id TEXT PRIMARY KEY,
                  draft_key TEXT NOT NULL UNIQUE,
                  status TEXT NOT NULL,
                  snapshot_id TEXT NOT NULL,
                  report_hash TEXT NOT NULL,
                  destination_id TEXT NOT NULL,
                  audience_json TEXT NOT NULL,
                  classification TEXT NOT NULL,
                  source_rights_ref TEXT NOT NULL,
                  purpose TEXT NOT NULL,
                  retention_days INTEGER NOT NULL,
                  redaction_profile TEXT NOT NULL,
                  scheduled_for TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  policy_decision_ref TEXT,
                  approval_ref TEXT,
                  approved_at TEXT,
                  policy_expires_at TEXT,
                  destination_verified_until TEXT
                );
                CREATE TABLE IF NOT EXISTS outbox (
                  outbox_id TEXT PRIMARY KEY,
                  subscription_id TEXT NOT NULL UNIQUE REFERENCES subscriptions(subscription_id),
                  idempotency_key TEXT NOT NULL UNIQUE,
                  status TEXT NOT NULL,
                  available_at TEXT NOT NULL,
                  claim_expires_at TEXT,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  max_attempts INTEGER NOT NULL,
                  delivery_hash TEXT NOT NULL,
                  activation_hash TEXT NOT NULL,
                  last_failure_code TEXT
                );
                CREATE TABLE IF NOT EXISTS receipts (
                  receipt_id TEXT PRIMARY KEY,
                  outbox_id TEXT NOT NULL UNIQUE REFERENCES outbox(outbox_id),
                  subscription_id TEXT NOT NULL REFERENCES subscriptions(subscription_id),
                  destination_id TEXT NOT NULL,
                  report_hash TEXT NOT NULL,
                  delivery_hash TEXT NOT NULL,
                  delivered_at TEXT NOT NULL,
                  retained_until TEXT NOT NULL,
                  simulation INTEGER NOT NULL CHECK(simulation = 1)
                );
                CREATE TABLE IF NOT EXISTS audit (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                  event_id TEXT NOT NULL UNIQUE,
                  event_type TEXT NOT NULL,
                  subscription_id TEXT NOT NULL,
                  outbox_id TEXT,
                  occurred_at TEXT NOT NULL,
                  evidence_ref TEXT
                );
                COMMIT;
                """
            )
        os.chmod(self.database, 0o600)

    @staticmethod
    def _audit(
        connection,
        event_type: str,
        subscription_id: str,
        occurred_at: str,
        *,
        outbox_id: str | None = None,
        evidence_ref: str | None = None,
    ) -> None:
        sequence = connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM audit").fetchone()[
            0
        ]
        identity = [sequence, event_type, subscription_id, outbox_id, occurred_at, evidence_ref]
        connection.execute(
            "INSERT INTO audit(event_id,event_type,subscription_id,outbox_id,occurred_at,evidence_ref) VALUES(?,?,?,?,?,?)",
            (
                "evt-" + hashlib.sha256(_canonical(identity)).hexdigest()[:24],
                event_type,
                subscription_id,
                outbox_id,
                occurred_at,
                evidence_ref,
            ),
        )

    def create_draft(
        self,
        *,
        snapshot_id: str,
        destination_id: str,
        audience: tuple[str, ...],
        classification: str,
        source_rights_ref: str,
        purpose: str,
        retention_days: int,
        redaction_profile: str,
        scheduled_for: str,
        expires_at: str,
        draft_key: str,
        now: str,
    ) -> dict:
        snapshot = _safe_snapshot(self.snapshot_store.get(snapshot_id))
        destination_id = _safe(destination_id, "destination_id", identifier=True)
        source_rights_ref = _safe(source_rights_ref, "source_rights_ref")
        purpose = _safe(purpose, "purpose", identifier=True)
        draft_key = _safe(draft_key, "draft_key")
        if (
            not isinstance(audience, tuple)
            or not audience
            or tuple(snapshot["audience"]) != audience
            or any(
                not isinstance(value, str)
                or not value
                or len(value) > 128
                or _SENSITIVE.search(value)
                for value in audience
            )
        ):
            raise ReportDeliveryError("audience is invalid or differs from the frozen report")
        if classification not in CLASSIFICATIONS or classification != _snapshot_classification(
            snapshot
        ):
            raise ReportDeliveryError("classification differs from the frozen report")
        if redaction_profile not in REDACTION_PROFILES:
            raise ReportDeliveryError("redaction profile is invalid")
        if (
            classification in {"confidential", "restricted"}
            and redaction_profile != "metadata_only"
        ):
            raise ReportDeliveryError("sensitive classifications require metadata-only simulation")
        if (
            not isinstance(retention_days, int)
            or isinstance(retention_days, bool)
            or not 1 <= retention_days <= 3650
        ):
            raise ReportDeliveryError("retention_days must be between 1 and 3650")
        scheduled = _time(scheduled_for, "scheduled_for")
        expires = _time(expires_at, "expires_at")
        created = _time(now, "now")
        if not created <= scheduled < expires:
            raise ReportDeliveryError("schedule must be current and precede expiry")
        config = {
            "snapshot_id": snapshot_id,
            "report_hash": snapshot["report_hash"],
            "destination_id": destination_id,
            "audience": audience,
            "classification": classification,
            "source_rights_ref": source_rights_ref,
            "purpose": purpose,
            "retention_days": retention_days,
            "redaction_profile": redaction_profile,
            "scheduled_for": _stamp(scheduled),
            "expires_at": _stamp(expires),
        }
        subscription_id = (
            "sub-"
            + hashlib.sha256(_canonical({**config, "draft_key": draft_key})).hexdigest()[:24]
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT * FROM subscriptions WHERE draft_key=?", (draft_key,)
            ).fetchone()
            if prior is not None:
                if prior["subscription_id"] != subscription_id:
                    connection.rollback()
                    raise ReportDeliveryConflict("draft idempotency key conflicts")
                connection.commit()
                return self.get_subscription(subscription_id)
            connection.execute(
                """INSERT INTO subscriptions(
                subscription_id,draft_key,status,snapshot_id,report_hash,destination_id,
                audience_json,classification,source_rights_ref,purpose,retention_days,
                redaction_profile,scheduled_for,expires_at,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    subscription_id,
                    draft_key,
                    "disabled",
                    snapshot_id,
                    snapshot["report_hash"],
                    destination_id,
                    json.dumps(audience, separators=(",", ":")),
                    classification,
                    source_rights_ref,
                    purpose,
                    retention_days,
                    redaction_profile,
                    _stamp(scheduled),
                    _stamp(expires),
                    _stamp(created),
                ),
            )
            self._audit(connection, "subscription_drafted", subscription_id, _stamp(created))
            connection.commit()
        return self.get_subscription(subscription_id)

    def _validate_approval(
        self, row: sqlite3.Row, approval: DeliveryApproval, now: datetime
    ) -> None:
        expected = {
            "report_hash": row["report_hash"],
            "destination_id": row["destination_id"],
            "audience": tuple(json.loads(row["audience_json"])),
            "classification": row["classification"],
            "source_rights_ref": row["source_rights_ref"],
            "purpose": row["purpose"],
            "retention_days": row["retention_days"],
            "redaction_profile": row["redaction_profile"],
        }
        if any(getattr(approval, key) != value for key, value in expected.items()):
            raise ReportDeliveryDenied("approval does not bind the exact delivery")
        for field in ("policy_decision_ref", "approval_ref"):
            _safe(getattr(approval, field), field)
        if (
            approval.capability != DELIVERY_CAPABILITY
            or approval.policy_state != "allow"
            or approval.approval_state != "approved"
            or approval.destination_state != "verified"
            or approval.source_rights_state != "approved"
            or approval.simulation_only is not True
        ):
            raise ReportDeliveryDenied("approval states do not authorize simulation")
        approved = _time(approval.approved_at, "approved_at")
        policy_expires = _time(approval.expires_at, "approval expires_at")
        destination_expires = _time(
            approval.destination_verified_until, "destination_verified_until"
        )
        scheduled = _time(row["scheduled_for"], "scheduled_for")
        subscription_expires = _time(row["expires_at"], "subscription expires_at")
        if now >= subscription_expires:
            raise ReportDeliveryDenied("subscription expired before activation")
        if not approved <= now < policy_expires or scheduled >= policy_expires:
            raise ReportDeliveryDenied("policy approval is expired or cannot cover the schedule")
        if scheduled >= destination_expires:
            raise ReportDeliveryDenied("destination verification cannot cover the schedule")
        try:
            allowed = self.policy_checker(approval)
        except Exception:
            allowed = False
        if allowed is not True:
            raise ReportDeliveryDenied("CapAuth policy denied report delivery simulation")

    def activate(
        self,
        subscription_id: str,
        approval: DeliveryApproval,
        *,
        idempotency_key: str,
        now: str,
    ) -> dict:
        subscription_id = _safe(subscription_id, "subscription_id", identifier=True)
        idempotency_key = _safe(idempotency_key, "idempotency_key")
        current = _time(now, "now")
        activation_hash = _digest(
            {
                "subscription_id": subscription_id,
                "idempotency_key": idempotency_key,
                "approval": asdict(approval),
            }
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT subscription_id,activation_hash FROM outbox WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["subscription_id"] != subscription_id
                    or existing["activation_hash"] != activation_hash
                ):
                    connection.rollback()
                    raise ReportDeliveryConflict("activation idempotency key conflicts")
                connection.commit()
                return self.get_subscription(subscription_id)
            row = connection.execute(
                "SELECT * FROM subscriptions WHERE subscription_id=?", (subscription_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(subscription_id)
            if row["status"] != "disabled":
                connection.rollback()
                raise ReportDeliveryConflict("subscription cannot be activated again")
            try:
                self._validate_approval(row, approval, current)
            except ReportDeliveryDenied:
                self._audit(
                    connection,
                    "activation_denied",
                    subscription_id,
                    _stamp(current),
                    evidence_ref=(
                        approval.policy_decision_ref
                        if isinstance(approval.policy_decision_ref, str)
                        and _REF.fullmatch(approval.policy_decision_ref)
                        and not _SENSITIVE.search(approval.policy_decision_ref)
                        else None
                    ),
                )
                connection.commit()
                raise
            outbox_id = (
                "out-"
                + hashlib.sha256(f"{subscription_id}:{idempotency_key}".encode()).hexdigest()[:24]
            )
            delivery_hash = _digest(
                {
                    "subscription_id": subscription_id,
                    "report_hash": row["report_hash"],
                    "destination_id": row["destination_id"],
                    "audience": json.loads(row["audience_json"]),
                    "classification": row["classification"],
                    "purpose": row["purpose"],
                    "retention_days": row["retention_days"],
                    "redaction_profile": row["redaction_profile"],
                }
            )
            connection.execute(
                """UPDATE subscriptions SET status='active',policy_decision_ref=?,approval_ref=?,
                approved_at=?,policy_expires_at=?,destination_verified_until=?
                WHERE subscription_id=?""",
                (
                    approval.policy_decision_ref,
                    approval.approval_ref,
                    _stamp(_time(approval.approved_at, "approved_at")),
                    _stamp(_time(approval.expires_at, "approval expires_at")),
                    _stamp(
                        _time(
                            approval.destination_verified_until,
                            "destination_verified_until",
                        )
                    ),
                    subscription_id,
                ),
            )
            connection.execute(
                """INSERT INTO outbox(outbox_id,subscription_id,idempotency_key,status,
                available_at,max_attempts,delivery_hash,activation_hash)
                VALUES(?,?,?,'queued',?,?,?,?)""",
                (
                    outbox_id,
                    subscription_id,
                    idempotency_key,
                    row["scheduled_for"],
                    MAX_ATTEMPTS,
                    delivery_hash,
                    activation_hash,
                ),
            )
            self._audit(
                connection,
                "subscription_activated",
                subscription_id,
                _stamp(current),
                outbox_id=outbox_id,
                evidence_ref=approval.approval_ref,
            )
            connection.commit()
        return self.get_subscription(subscription_id)

    def _payload(self, row: sqlite3.Row, snapshot: dict) -> bytes:
        if row["redaction_profile"] == "metadata_only":
            value = {
                "snapshot_id": snapshot["snapshot_id"],
                "report_hash": snapshot["report_hash"],
                "report_type": snapshot["report_type"],
                "classification": row["classification"],
            }
        else:
            value = snapshot
        payload = _canonical(value)
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise ReportDeliveryError("simulation payload exceeds its bound")
        return payload

    def dispatch_due(
        self,
        destination: SimulationDestination,
        *,
        now: str,
        limit: int = 10,
    ) -> list[dict]:
        if (
            type(destination) is not SimulationDestination
            or destination.simulation_only is not True
        ):
            raise ReportDeliveryDenied("only the built-in simulation destination is allowed")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_BATCH:
            raise ReportDeliveryError("dispatch limit is invalid")
        current = _time(now, "now")
        results = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            unknown = connection.execute(
                """SELECT outbox_id,subscription_id FROM outbox
                WHERE status='dispatching' AND claim_expires_at<=?""",
                (_stamp(current),),
            ).fetchall()
            for claim in unknown:
                connection.execute(
                    """UPDATE outbox SET status='failed',claim_expires_at=NULL,
                    last_failure_code='DELIVERY_OUTCOME_UNKNOWN' WHERE outbox_id=?""",
                    (claim["outbox_id"],),
                )
                connection.execute(
                    "UPDATE subscriptions SET status='failed' WHERE subscription_id=?",
                    (claim["subscription_id"],),
                )
                self._audit(
                    connection,
                    "delivery_outcome_unknown",
                    claim["subscription_id"],
                    _stamp(current),
                    outbox_id=claim["outbox_id"],
                )
            connection.commit()
            results.extend(self.get_subscription(claim["subscription_id"]) for claim in unknown)
            rows = connection.execute(
                """SELECT o.*,s.* FROM outbox o JOIN subscriptions s USING(subscription_id)
                WHERE o.status IN ('queued','retry_wait') AND o.available_at<=?
                AND s.destination_id=? ORDER BY o.available_at,o.outbox_id LIMIT ?""",
                (_stamp(current), destination.destination_id, limit),
            ).fetchall()
        for row in rows:
            results.append(self._dispatch_one(row["outbox_id"], destination, current))
        return results

    def _dispatch_one(
        self, outbox_id: str, destination: SimulationDestination, current: datetime
    ) -> dict:
        now = _stamp(current)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT o.*,s.* FROM outbox o JOIN subscriptions s USING(subscription_id) WHERE outbox_id=?",
                (outbox_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(outbox_id)
            if row["status"] not in {"queued", "retry_wait"}:
                connection.commit()
                return self.get_subscription(row["subscription_id"])
            if (
                current >= _time(row["expires_at"], "expires_at")
                or current >= _time(row["policy_expires_at"], "policy_expires_at")
                or current
                >= _time(row["destination_verified_until"], "destination_verified_until")
            ):
                connection.execute(
                    "UPDATE outbox SET status='expired' WHERE outbox_id=?", (outbox_id,)
                )
                connection.execute(
                    "UPDATE subscriptions SET status='expired' WHERE subscription_id=?",
                    (row["subscription_id"],),
                )
                self._audit(
                    connection,
                    "delivery_expired",
                    row["subscription_id"],
                    now,
                    outbox_id=outbox_id,
                )
                connection.commit()
                return self.get_subscription(row["subscription_id"])
            attempts = row["attempts"] + 1
            claimed = connection.execute(
                """UPDATE outbox SET status='dispatching',attempts=?,claim_expires_at=?
                WHERE outbox_id=? AND status IN ('queued','retry_wait')""",
                (
                    attempts,
                    _stamp(current + timedelta(seconds=CLAIM_SECONDS)),
                    outbox_id,
                ),
            )
            if claimed.rowcount != 1:
                connection.commit()
                return self.get_subscription(row["subscription_id"])
            connection.commit()
        try:
            current_approval = DeliveryApproval(
                report_hash=row["report_hash"],
                destination_id=row["destination_id"],
                audience=tuple(json.loads(row["audience_json"])),
                classification=row["classification"],
                source_rights_ref=row["source_rights_ref"],
                purpose=row["purpose"],
                retention_days=row["retention_days"],
                redaction_profile=row["redaction_profile"],
                policy_decision_ref=row["policy_decision_ref"],
                approval_ref=row["approval_ref"],
                approved_at=row["approved_at"],
                expires_at=row["policy_expires_at"],
                destination_verified_until=row["destination_verified_until"],
            )
            try:
                policy_current = self.policy_checker(current_approval)
            except Exception:
                policy_current = False
            if policy_current is not True:
                raise ReportDeliveryDenied("CapAuth policy denied dispatch simulation")
            snapshot = _safe_snapshot(self.snapshot_store.get(row["snapshot_id"]))
            if snapshot["report_hash"] != row["report_hash"]:
                raise ReportDeliveryConflict("frozen report hash changed")
            payload = self._payload(row, snapshot)
            receipt = SimulationDestination.send(
                destination,
                idempotency_key=row["idempotency_key"],
                payload=payload,
                metadata={
                    "outbox_id": outbox_id,
                    "subscription_id": row["subscription_id"],
                    "report_hash": row["report_hash"],
                    "destination_id": row["destination_id"],
                    "delivery_hash": row["delivery_hash"],
                },
                delivered_at=now,
            )
            if (
                set(receipt)
                != {
                    "receipt_id",
                    "destination_id",
                    "delivery_hash",
                    "delivered_at",
                    "simulation",
                }
                or receipt["destination_id"] != row["destination_id"]
                or receipt["delivery_hash"] != row["delivery_hash"]
                or receipt["delivered_at"] != now
                or receipt["simulation"] is not True
                or not _ID.fullmatch(receipt["receipt_id"])
            ):
                raise ReportDeliveryConflict("simulation receipt is invalid")
        except SimulationDestinationError:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                status = "retry_wait" if attempts < row["max_attempts"] else "failed"
                available = _stamp(current + timedelta(seconds=60 * 2 ** (attempts - 1)))
                connection.execute(
                    """UPDATE outbox SET status=?,available_at=?,claim_expires_at=NULL,
                    last_failure_code='DESTINATION_ERROR' WHERE outbox_id=?""",
                    (status, available, outbox_id),
                )
                if status == "failed":
                    connection.execute(
                        "UPDATE subscriptions SET status='failed' WHERE subscription_id=?",
                        (row["subscription_id"],),
                    )
                self._audit(
                    connection,
                    "delivery_retry_scheduled" if status == "retry_wait" else "delivery_failed",
                    row["subscription_id"],
                    now,
                    outbox_id=outbox_id,
                )
                connection.commit()
            return self.get_subscription(row["subscription_id"])
        except ReportDeliveryError:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """UPDATE outbox SET status='failed',claim_expires_at=NULL,
                    last_failure_code='POLICY_OR_INTEGRITY_DENIED' WHERE outbox_id=?""",
                    (outbox_id,),
                )
                connection.execute(
                    "UPDATE subscriptions SET status='failed' WHERE subscription_id=?",
                    (row["subscription_id"],),
                )
                self._audit(
                    connection,
                    "delivery_denied",
                    row["subscription_id"],
                    now,
                    outbox_id=outbox_id,
                )
                connection.commit()
            return self.get_subscription(row["subscription_id"])
        retained_until = _stamp(current + timedelta(days=row["retention_days"]))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO receipts(receipt_id,outbox_id,subscription_id,
                destination_id,report_hash,delivery_hash,delivered_at,retained_until,simulation)
                VALUES(?,?,?,?,?,?,?,?,1)""",
                (
                    receipt["receipt_id"],
                    outbox_id,
                    row["subscription_id"],
                    row["destination_id"],
                    row["report_hash"],
                    receipt["delivery_hash"],
                    receipt["delivered_at"],
                    retained_until,
                ),
            )
            stored = connection.execute(
                """SELECT receipt_id,outbox_id,subscription_id,destination_id,report_hash,
                delivery_hash,delivered_at,retained_until,simulation FROM receipts
                WHERE outbox_id=?""",
                (outbox_id,),
            ).fetchone()
            expected_receipt = {
                "receipt_id": receipt["receipt_id"],
                "outbox_id": outbox_id,
                "subscription_id": row["subscription_id"],
                "destination_id": row["destination_id"],
                "report_hash": row["report_hash"],
                "delivery_hash": receipt["delivery_hash"],
                "delivered_at": receipt["delivered_at"],
                "retained_until": retained_until,
                "simulation": 1,
            }
            if dict(stored) != expected_receipt:
                connection.rollback()
                raise ReportDeliveryConflict("stored receipt does not bind exact delivery")
            connection.execute(
                """UPDATE outbox SET status='receipt_verified',claim_expires_at=NULL,
                last_failure_code=NULL WHERE outbox_id=?""",
                (outbox_id,),
            )
            connection.execute(
                "UPDATE subscriptions SET status='receipt_verified' WHERE subscription_id=?",
                (row["subscription_id"],),
            )
            self._audit(
                connection,
                "receipt_verified",
                row["subscription_id"],
                now,
                outbox_id=outbox_id,
                evidence_ref=receipt["receipt_id"],
            )
            connection.commit()
        return self.get_subscription(row["subscription_id"])

    def cancel(self, subscription_id: str, *, cancellation_ref: str, now: str) -> dict:
        subscription_id = _safe(subscription_id, "subscription_id", identifier=True)
        cancellation_ref = _safe(cancellation_ref, "cancellation_ref")
        occurred = _stamp(_time(now, "now"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM subscriptions WHERE subscription_id=?", (subscription_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(subscription_id)
            if row["status"] in {"receipt_verified", "failed", "expired"}:
                connection.rollback()
                raise ReportDeliveryConflict("terminal delivery cannot be cancelled")
            if row["status"] != "cancelled":
                claimed = connection.execute(
                    "SELECT 1 FROM outbox WHERE subscription_id=? AND status='dispatching'",
                    (subscription_id,),
                ).fetchone()
                if claimed is not None:
                    connection.rollback()
                    raise ReportDeliveryConflict("in-flight delivery cannot be cancelled")
                connection.execute(
                    "UPDATE subscriptions SET status='cancelled' WHERE subscription_id=?",
                    (subscription_id,),
                )
                connection.execute(
                    "UPDATE outbox SET status='cancelled' WHERE subscription_id=? AND status IN ('queued','retry_wait')",
                    (subscription_id,),
                )
                outbox = connection.execute(
                    "SELECT outbox_id FROM outbox WHERE subscription_id=?", (subscription_id,)
                ).fetchone()
                self._audit(
                    connection,
                    "subscription_cancelled",
                    subscription_id,
                    occurred,
                    outbox_id=outbox["outbox_id"] if outbox else None,
                    evidence_ref=cancellation_ref,
                )
            connection.commit()
        return self.get_subscription(subscription_id)

    unsubscribe = cancel

    def get_subscription(self, subscription_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscriptions WHERE subscription_id=?", (subscription_id,)
            ).fetchone()
            if row is None:
                raise KeyError(subscription_id)
            outbox = connection.execute(
                """SELECT outbox_id,status,available_at,claim_expires_at,attempts,
                max_attempts,delivery_hash,last_failure_code FROM outbox
                WHERE subscription_id=?""",
                (subscription_id,),
            ).fetchone()
            receipt = connection.execute(
                "SELECT receipt_id,destination_id,report_hash,delivery_hash,delivered_at,retained_until,simulation FROM receipts WHERE subscription_id=?",
                (subscription_id,),
            ).fetchone()
        return {
            "subscription_id": row["subscription_id"],
            "status": row["status"],
            "snapshot_id": row["snapshot_id"],
            "report_hash": row["report_hash"],
            "destination_id": row["destination_id"],
            "audience": json.loads(row["audience_json"]),
            "classification": row["classification"],
            "source_rights_ref": row["source_rights_ref"],
            "purpose": row["purpose"],
            "retention_days": row["retention_days"],
            "redaction_profile": row["redaction_profile"],
            "scheduled_for": row["scheduled_for"],
            "expires_at": row["expires_at"],
            "simulation_only": True,
            "external_delivery_authorized": False,
            "outbox": dict(outbox) if outbox else None,
            "receipt": dict(receipt) if receipt else None,
        }

    def audit_events(self, subscription_id: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence,event_id,event_type,subscription_id,outbox_id,occurred_at,evidence_ref FROM audit WHERE subscription_id=? ORDER BY sequence",
                (subscription_id,),
            ).fetchall()
        return [dict(row) for row in rows]


__all__ = [
    "DELIVERY_CAPABILITY",
    "DeliveryApproval",
    "ReportDeliveryConflict",
    "ReportDeliveryDenied",
    "ReportDeliveryError",
    "ReportDeliveryService",
    "SimulationDestination",
    "SimulationDestinationError",
]
