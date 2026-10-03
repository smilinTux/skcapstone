"""Bounded, metadata-only contracts for the SKRSI registry.

This module deliberately has no runtime, network, model, or capability
dependency.  It is the source-only contract boundary for the first SKRSI
wave.  Records are canonicalized before they are hashed or appended to the
outbox, and unknown or unsafe input is rejected rather than guessed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA = "skrsi.target.v1"
EVENT_SCHEMA = "skrsi.event.v1"
RECORD_TYPES = frozenset(
    {
        "Observation",
        "Baseline",
        "Hypothesis",
        "Experiment",
        "InterventionProposal",
        "Evaluation",
        "Decision",
        "Rollout",
        "Regression",
        "Recovery",
    }
)
REQUIRED_PAYLOAD_FIELDS = {
    "Observation": {"value", "unit", "source", "cohort", "sample_id", "collection_quality"},
    "Baseline": {"frozen_window", "population", "metric_distribution", "sample_size"},
    "Hypothesis": {"expected_delta", "mechanism", "guardrails", "prior"},
    "Experiment": {"cohorts", "randomization", "target_revision", "stop_rules", "budget"},
    "InterventionProposal": {
        "proposed_change",
        "artifact_hash",
        "permissions",
        "blast_radius",
        "rollback",
        "required_reviewers",
    },
    "Evaluation": {
        "evaluator_version",
        "deterministic_checks",
        "independent_reviewer",
        "metric_results",
        "confidence_interval",
        "power",
        "sample_adequacy",
        "negative_findings",
    },
    "Decision": {"verdict", "decision_policy", "rationale_digest", "expires_at", "approver"},
    "Rollout": {"proposal_approval", "bounded_observation"},
    "Regression": {
        "invariant_or_slo",
        "affected_cohort",
        "first_observed",
        "severity",
        "containment_proposal",
    },
    "Recovery": {"trigger", "owner", "resulting_health", "verification_hash"},
}
TARGET_STATUSES = frozenset({"draft", "approved", "active", "paused", "expired", "retired"})
RISK_CLASSES = frozenset({"low", "medium", "high", "critical"})
LOGICAL_ROUTES = frozenset({"sk-s", "sk-m", "sk-l", "sk-xl"})
FORBIDDEN_KEYS = frozenset(
    {
        "prompt",
        "prompts",
        "response",
        "responses",
        "body",
        "content",
        "matter",
        "inbox",
        "corpus",
        "secret",
        "secrets",
        "token",
        "tokens",
        "credential",
        "credentials",
        "password",
        "private_key",
        "capability_grant",
        "capability_grants",
    }
)
REDACTED = "[REDACTED]"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,159}$")


class SKRSIError(ValueError):
    """Base error for invalid or unsafe SKRSI contract data."""


class SKRSIIntegrityError(SKRSIError):
    """Raised when canonical bytes or an immutable hash does not match."""


def _utc(value: str | datetime) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise SKRSIError("timestamps must include a timezone")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str):
        raise SKRSIError("timestamp must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SKRSIError("timestamp is not ISO-8601") from exc
    return _utc(parsed)


def _id(value: str, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise SKRSIError(f"{name} is invalid")
    return value


def _finite(value: Any, path: str = "value") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise SKRSIError(f"{path} must be finite")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _finite(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _finite(item, f"{path}[{index}]")


def canonical_json(value: Any) -> bytes:
    """Return deterministic JSON bytes and reject non-finite numbers."""

    _finite(value)
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SKRSIError("value is not canonically serializable") from exc


def sha256(value: bytes | str) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def redact_metadata(value: Any) -> Any:
    """Copy metadata while redacting protected values before persistence."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SKRSIError("metadata keys must be strings")
            if key.lower() in FORBIDDEN_KEYS or any(
                part in key.lower() for part in ("secret", "token", "password")
            ):
                result[key] = REDACTED
            else:
                result[key] = redact_metadata(item)
        return result
    if isinstance(value, (list, tuple)):
        return [redact_metadata(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise SKRSIError("metadata contains an unsupported value")


def _safe_metadata(value: Mapping[str, Any], field_name: str = "metadata") -> dict[str, Any]:
    clean = redact_metadata(value)
    if not isinstance(clean, dict):
        raise SKRSIError(f"{field_name} must be an object")
    return clean


def _tuple(value: Iterable[Any], name: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)):
        raise SKRSIError(f"{name} must be a sequence")
    return tuple(copy.deepcopy(item) for item in value)


@dataclass(frozen=True)
class TargetRevision:
    """An immutable, metadata-only SKRSI target revision."""

    target_id: str
    revision: int
    owner: str
    domain: str
    objective: str
    scope: Mapping[str, Any]
    invariants: tuple[str, ...]
    baseline_window: Mapping[str, Any]
    metrics: tuple[str, ...]
    slos: Mapping[str, Any]
    evaluators: tuple[str, ...]
    permitted_interventions: tuple[str, ...]
    exclusions: tuple[str, ...]
    risk_class: str
    review_policy: Mapping[str, Any]
    activation: Mapping[str, Any]
    rollback: Mapping[str, Any]
    schema: str = SCHEMA
    status: str = "draft"
    _canonical: bytes = field(init=False, repr=False, compare=False)
    _hash: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _id(self.target_id, "target_id")
        _id(self.owner, "owner")
        _id(self.domain, "domain")
        if self.schema != SCHEMA or not isinstance(self.revision, int) or self.revision < 1:
            raise SKRSIError("target schema or revision is invalid")
        if self.status not in TARGET_STATUSES or self.risk_class not in RISK_CLASSES:
            raise SKRSIError("target status or risk class is invalid")
        if not self.objective.strip() or not self.scope:
            raise SKRSIError("target objective and scope are required")
        invariants = _tuple(self.invariants, "invariants")
        exclusions = _tuple(self.exclusions, "exclusions")
        if not invariants or not exclusions:
            raise SKRSIError("invariants and exclusions are required")
        required_exclusions = {"capability_grants", "production_activation", "protected_data"}
        if not required_exclusions.issubset(set(exclusions)):
            raise SKRSIError(
                "target exclusions must deny capabilities, activation, and protected data"
            )
        metrics = _tuple(self.metrics, "metrics")
        evaluators = _tuple(self.evaluators, "evaluators")
        interventions = _tuple(self.permitted_interventions, "permitted_interventions")
        if not metrics or not evaluators:
            raise SKRSIError("metrics and evaluators are required")
        scope = _safe_metadata(self.scope, "scope")
        baseline = _safe_metadata(self.baseline_window, "baseline_window")
        slos = _safe_metadata(self.slos, "slos")
        policy = _safe_metadata(self.review_policy, "review_policy")
        activation = _safe_metadata(self.activation, "activation")
        rollback = _safe_metadata(self.rollback, "rollback")
        if int(baseline.get("min_samples", 0)) < 1:
            raise SKRSIError("baseline_window.min_samples must be positive")
        if not rollback.get("artifact_ref") or not rollback.get("trigger"):
            raise SKRSIError("rollback artifact and trigger are required")
        if not activation.get("expires_at"):
            raise SKRSIError("target expiry is required")
        expiry = _utc(activation["expires_at"])
        if datetime.fromisoformat(expiry.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
            raise SKRSIError("target expiry must be in the future")
        if self.risk_class in {"high", "critical"} and not policy.get("human_approval_for"):
            raise SKRSIError("high and critical risk require an explicit review policy")
        data = {
            "schema": self.schema,
            "id": self.target_id,
            "revision": self.revision,
            "status": self.status,
            "owner": self.owner,
            "domain": self.domain,
            "scope": scope,
            "objective": self.objective,
            "invariants": list(invariants),
            "baseline_window": baseline,
            "metrics": list(metrics),
            "slos": slos,
            "evaluators": list(evaluators),
            "permitted_interventions": list(interventions),
            "exclusions": list(exclusions),
            "risk_class": self.risk_class,
            "review_policy": policy,
            "activation": {**activation, "expires_at": expiry},
            "rollback": rollback,
        }
        encoded = canonical_json(data)
        object.__setattr__(self, "_canonical", encoded)
        object.__setattr__(self, "_hash", sha256(encoded))

    @property
    def target_ref(self) -> str:
        return f"{self.target_id}@{self.revision}"

    @property
    def content_hash(self) -> str:
        return self._hash

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._canonical)

    def canonical_bytes(self) -> bytes:
        return self._canonical

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TargetRevision":
        if data.get("schema") != SCHEMA:
            raise SKRSIError("unknown target schema")
        values = dict(data)
        values["target_id"] = values.pop("id")
        return cls(**values)


@dataclass(frozen=True)
class MetadataRecord:
    """One typed SKRSI architecture record, with a hash-bound envelope."""

    record_type: str
    event_id: str
    event_type: str
    occurred_at: str
    recorded_at: str
    actor: str
    target_ref: str
    payload: Mapping[str, Any]
    correlation_id: str = ""
    causation_id: str = ""
    card_refs: tuple[str, ...] = ()
    agent_ref: str = ""
    host_ref: str = ""
    route_ref: str = ""
    commit_ref: str = ""
    evidence_refs: tuple[str, ...] = ()
    mail_refs: tuple[str, ...] = ()
    experiment_ref: str = ""
    redaction_class: str = "metadata-only"
    policy_revision: str = "skrsi-policy.v1"
    schema: str = EVENT_SCHEMA
    _canonical: bytes = field(init=False, repr=False, compare=False)
    _hash: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.schema != EVENT_SCHEMA or self.record_type not in RECORD_TYPES:
            raise SKRSIError("unknown event schema or record type")
        _id(self.event_id, "event_id")
        _id(self.event_type, "event_type")
        _id(self.actor, "actor")
        _id(self.target_ref, "target_ref")
        occurred = _utc(self.occurred_at)
        recorded = _utc(self.recorded_at)
        payload = _safe_metadata(self.payload, "payload")
        if self.redaction_class != "metadata-only":
            raise SKRSIError("only metadata-only records are permitted")
        missing = REQUIRED_PAYLOAD_FIELDS[self.record_type] - set(payload)
        if missing:
            raise SKRSIError(
                f"{self.record_type} payload is missing: {', '.join(sorted(missing))}"
            )
        route = self.route_ref
        if route and route not in LOGICAL_ROUTES:
            raise SKRSIError("route_ref must be a provider-neutral logical route")
        card_refs = _tuple(self.card_refs, "card_refs")
        evidence_refs = _tuple(self.evidence_refs, "evidence_refs")
        mail_refs = _tuple(self.mail_refs, "mail_refs")
        for name, refs in (
            ("card_refs", card_refs),
            ("evidence_refs", evidence_refs),
            ("mail_refs", mail_refs),
        ):
            for ref in refs:
                _id(ref, name)
        if self.record_type == "Decision":
            verdict = payload.get("verdict")
            if verdict not in {"PASS", "PASS_FOR_REVIEW", "BLOCKED"}:
                raise SKRSIError("decision verdict is invalid")
            if verdict == "BLOCKED":
                blocked = payload.get("blocked_on")
                if not isinstance(blocked, Mapping) or set(blocked) != {"kind", "referent"}:
                    raise SKRSIError("BLOCKED decision requires exactly one blocked_on referent")
                if blocked["kind"] not in {"dependency", "human", "capability", "card"}:
                    raise SKRSIError("blocked_on kind is invalid")
        if self.record_type == "InterventionProposal" and payload.get("execute") is True:
            raise SKRSIError("intervention proposals cannot execute")
        if self.record_type == "Rollout" and payload.get("production_activation") is True:
            raise SKRSIError("rollout records cannot activate production")
        if self.record_type == "Evaluation" and payload.get("independent_reviewer") == self.actor:
            raise SKRSIError("evaluator and independent reviewer must be distinct")
        data = {
            "schema": self.schema,
            "record_type": self.record_type,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "occurred_at": occurred,
            "recorded_at": recorded,
            "actor": self.actor,
            "target_ref": self.target_ref,
            "experiment_ref": self.experiment_ref,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "card_refs": list(card_refs),
            "agent_ref": self.agent_ref,
            "host_ref": self.host_ref,
            "route_ref": route,
            "commit_ref": self.commit_ref,
            "evidence_refs": list(evidence_refs),
            "mail_refs": list(mail_refs),
            "payload": payload,
            "payload_digest": sha256(canonical_json(payload)),
            "redaction_class": self.redaction_class,
            "policy_revision": self.policy_revision,
        }
        encoded = canonical_json(data)
        object.__setattr__(self, "_canonical", encoded)
        object.__setattr__(self, "_hash", sha256(encoded))

    @property
    def content_hash(self) -> str:
        return self._hash

    @property
    def idempotency_key(self) -> str:
        natural_key = self.payload.get("natural_key") or self.event_id
        return ":".join((self.actor, self.event_type, str(natural_key), self.schema))

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._canonical)

    def canonical_bytes(self) -> bytes:
        return self._canonical

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MetadataRecord":
        if data.get("schema") != EVENT_SCHEMA or data.get("record_type") not in RECORD_TYPES:
            raise SKRSIError("unknown event schema or record type")
        values = dict(data)
        payload_digest = values.pop("payload_digest", None)
        values.pop("schema", None)
        values.pop("record_type", None)
        record = cls(schema=EVENT_SCHEMA, record_type=data["record_type"], **values)
        if payload_digest != record.to_dict()["payload_digest"]:
            raise SKRSIIntegrityError("payload digest does not match canonical payload")
        return record


def make_record(
    record_type: str, *, actor: str, target_ref: str, payload: Mapping[str, Any], **kwargs: Any
) -> MetadataRecord:
    """Construct a typed record with safe default timestamps and an event id."""

    now = datetime.now(timezone.utc)
    return MetadataRecord(
        record_type=record_type,
        event_id=kwargs.pop("event_id", str(uuid.uuid4())),
        event_type=kwargs.pop("event_type", f"skrsi.{record_type.lower()}"),
        occurred_at=kwargs.pop("occurred_at", now),
        recorded_at=kwargs.pop("recorded_at", now),
        actor=actor,
        target_ref=target_ref,
        payload=payload,
        **kwargs,
    )


@dataclass(frozen=True)
class OutboxEntry:
    record_hash: str
    idempotency_key: str
    destination: str
    serialized: bytes
    appended_at: str
    sent: bool = False


class AppendOnlyOutbox:
    """A single-writer, append-only JSONL outbox with retention visibility."""

    def __init__(
        self, path: str | os.PathLike[str] | None = None, *, retention_days: int = 395
    ) -> None:
        if retention_days < 1:
            raise SKRSIError("retention_days must be positive")
        self.path = Path(path) if path else None
        self.retention_days = retention_days
        self._entries: list[OutboxEntry] = []
        self._by_key: dict[str, OutboxEntry] = {}
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                self._load()

    def _load(self) -> None:
        assert self.path is not None
        try:
            lines = self.path.read_bytes().splitlines()
            for line in lines:
                data = json.loads(line)
                if data.get("kind") == "ack":
                    key = data["idempotency_key"]
                    prior = self._by_key.get(key)
                    if prior is None or prior.record_hash != data["record_hash"]:
                        raise SKRSIIntegrityError("outbox acknowledgement has no matching record")
                    acknowledged = OutboxEntry(**{**prior.__dict__, "sent": True})
                    self._by_key[key] = acknowledged
                    self._entries[self._entries.index(prior)] = acknowledged
                    continue
                if data.get("kind") is not None or data.get("sent", False):
                    raise SKRSIIntegrityError("outbox entry is not an append-only record")
                serialized = bytes.fromhex(data["serialized_hex"])
                record = MetadataRecord.from_dict(json.loads(serialized))
                if data["record_hash"] != record.content_hash:
                    raise SKRSIIntegrityError("outbox record hash does not match content")
                if data["idempotency_key"] != record.idempotency_key:
                    raise SKRSIIntegrityError("outbox idempotency key does not match content")
                entry = OutboxEntry(
                    record_hash=data["record_hash"],
                    idempotency_key=data["idempotency_key"],
                    destination=data["destination"],
                    serialized=serialized,
                    appended_at=data["appended_at"],
                )
                existing = self._by_key.get(entry.idempotency_key)
                if existing is not None:
                    if (
                        existing.record_hash != entry.record_hash
                        or existing.serialized != entry.serialized
                    ):
                        raise SKRSIIntegrityError("outbox idempotency key has conflicting records")
                    continue
                self._entries.append(entry)
                self._by_key[entry.idempotency_key] = entry
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SKRSIIntegrityError("outbox is malformed; refusing to continue") from exc

    def append(self, record: MetadataRecord, *, destination: str = "skcapstone") -> OutboxEntry:
        if not isinstance(record, MetadataRecord):
            raise SKRSIError("outbox accepts only validated metadata records")
        _id(destination, "destination")
        with self._lock:
            existing = self._by_key.get(record.idempotency_key)
            if existing:
                if existing.record_hash != record.content_hash:
                    raise SKRSIIntegrityError("idempotency key reused with different record")
                return existing
            entry = OutboxEntry(
                record_hash=record.content_hash,
                idempotency_key=record.idempotency_key,
                destination=destination,
                serialized=record.canonical_bytes(),
                appended_at=_utc(datetime.now(timezone.utc)),
            )
            if self.path:
                line = (
                    canonical_json(
                        {
                            "record_hash": entry.record_hash,
                            "idempotency_key": entry.idempotency_key,
                            "destination": entry.destination,
                            "serialized_hex": entry.serialized.hex(),
                            "appended_at": entry.appended_at,
                            "sent": False,
                        }
                    )
                    + b"\n"
                )
                with self.path.open("ab") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            self._entries.append(entry)
            self._by_key[entry.idempotency_key] = entry
            return entry

    def acknowledge(self, entry: OutboxEntry) -> None:
        """Append a durable sent cursor; never rewrites the original entry."""

        with self._lock:
            current = self._by_key.get(entry.idempotency_key)
            if current is None or current.record_hash != entry.record_hash:
                raise SKRSIIntegrityError("cannot acknowledge an unknown outbox entry")
            if current.sent:
                return
            if self.path:
                line = (
                    canonical_json(
                        {
                            "kind": "ack",
                            "record_hash": current.record_hash,
                            "idempotency_key": current.idempotency_key,
                            "acknowledged_at": _utc(datetime.now(timezone.utc)),
                        }
                    )
                    + b"\n"
                )
                with self.path.open("ab") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            acknowledged = OutboxEntry(**{**current.__dict__, "sent": True})
            self._by_key[current.idempotency_key] = acknowledged
            self._entries[self._entries.index(current)] = acknowledged

    def pending(self, *, now: datetime | None = None) -> tuple[OutboxEntry, ...]:
        now = now or datetime.now(timezone.utc)
        cutoff = now.timestamp() - self.retention_days * 86400
        return tuple(
            entry
            for entry in self._entries
            if not entry.sent
            and datetime.fromisoformat(entry.appended_at.replace("Z", "+00:00")).timestamp()
            >= cutoff
        )

    def entries(self) -> tuple[OutboxEntry, ...]:
        return tuple(self._entries)


class TargetRegistry:
    """In-memory append-only target registry backed by an append-only outbox."""

    def __init__(self, outbox: AppendOnlyOutbox | None = None) -> None:
        self.outbox = outbox or AppendOnlyOutbox()
        self._revisions: dict[tuple[str, int], TargetRevision] = {}
        self._latest: dict[str, int] = {}

    def register(self, target: TargetRevision, *, idempotency_key: str) -> TargetRevision:
        _id(idempotency_key, "idempotency_key")
        key = (target.target_id, target.revision)
        prior = self._revisions.get(key)
        if prior:
            if prior.content_hash != target.content_hash:
                raise SKRSIIntegrityError("target revision is immutable")
            return prior
        latest = self._latest.get(target.target_id, 0)
        if target.revision <= latest:
            raise SKRSIIntegrityError("target revisions must increase")
        self._revisions[key] = target
        self._latest[target.target_id] = target.revision
        record = make_record(
            "Observation",
            actor=target.owner,
            target_ref=target.target_ref,
            payload={
                "natural_key": idempotency_key,
                "value": 1,
                "unit": "registration",
                "source": "registry",
                "cohort": "baseline",
                "sample_id": idempotency_key,
                "collection_quality": "complete",
                "action": "target.registered",
                "target_hash": target.content_hash,
            },
        )
        self.outbox.append(record)
        return target

    def get(self, target_id: str, revision: int) -> TargetRevision:
        try:
            return self._revisions[(target_id, revision)]
        except KeyError as exc:
            raise SKRSIError("unknown target revision") from exc

    def discover(
        self, *, product: str, domain: str, event_type: str, now: datetime | None = None
    ) -> tuple[TargetRevision, ...]:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise SKRSIError("discovery time must include a timezone")
        result = []
        for target in self._revisions.values():
            if target.status != "active" or target.domain != domain:
                continue
            scope = target.to_dict()["scope"]
            if product not in scope.get("products", []) or event_type not in scope.get(
                "event_types", []
            ):
                continue
            expiry = datetime.fromisoformat(
                target.to_dict()["activation"]["expires_at"].replace("Z", "+00:00")
            )
            if expiry <= now.astimezone(timezone.utc):
                continue
            result.append(target)
        return tuple(sorted(result, key=lambda item: (item.target_id, item.revision)))
