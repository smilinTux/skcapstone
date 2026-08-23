"""Durable, append-only lifecycle records for ATLAS action intents.

The ledger is operational evidence, not an authorization mechanism.  It binds an
action to its condition, target, catalog generation, ITIL change, and CMDB CI and
then permits only the documented lifecycle transitions.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..atomic_io import atomic_write_text

SCHEMA = "skcapstone.atlas.action-intent/v1"
_ID_RE = re.compile(r"^ai-[0-9a-f]{24}$")


class ActionState(str, Enum):
    """States in the governed action lifecycle."""

    OBSERVED = "observed"
    DIAGNOSED = "diagnosed"
    PROPOSED = "proposed"
    AUTHORIZED = "authorized"
    EXECUTING = "executing"
    VERIFIED = "verified"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    ESCALATED = "escalated"


_TRANSITIONS: dict[ActionState, frozenset[ActionState]] = {
    ActionState.OBSERVED: frozenset({ActionState.DIAGNOSED}),
    ActionState.DIAGNOSED: frozenset({ActionState.PROPOSED}),
    ActionState.PROPOSED: frozenset({ActionState.AUTHORIZED}),
    ActionState.AUTHORIZED: frozenset({ActionState.EXECUTING}),
    ActionState.EXECUTING: frozenset({ActionState.VERIFIED, ActionState.FAILED}),
    ActionState.FAILED: frozenset({ActionState.ROLLED_BACK, ActionState.ESCALATED}),
    ActionState.VERIFIED: frozenset(),
    ActionState.ROLLED_BACK: frozenset(),
    ActionState.ESCALATED: frozenset(),
}


def _canonical(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def stable_intent_id(identity: dict[str, Any]) -> str:
    """Derive a stable, non-secret identifier from an intent identity."""
    return "ai-" + hashlib.sha256(_canonical(identity)).hexdigest()[:24]


class ActionIntent(BaseModel):
    """Immutable identity and governance bindings for one desired action."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_id: str = SCHEMA
    intent_id: str = ""
    condition_fingerprint: str = Field(min_length=1, max_length=256)
    application: str = Field(min_length=1, max_length=128)
    target_kind: str = Field(min_length=1, max_length=64)
    target_id: str = Field(min_length=1, max_length=256)
    action: str = Field(min_length=1, max_length=128)
    catalog_generation: str = Field(min_length=1, max_length=128)
    created_at: datetime
    itil_change_id: str | None = Field(default=None, max_length=128)
    cmdb_ci_id: str | None = Field(default=None, max_length=256)
    verification: dict[str, Any] = Field(default_factory=dict)
    rollback: dict[str, Any] = Field(default_factory=dict)
    authorization_ref: str | None = Field(default=None, min_length=1, max_length=1024)

    @field_validator("schema_id")
    @classmethod
    def _known_schema(cls, value: str) -> str:
        if value != SCHEMA:
            raise ValueError(f"unsupported action intent schema: {value}")
        return value

    def identity(self) -> dict[str, Any]:
        """Return the fields defining deduplication identity."""
        return {
            "condition_fingerprint": self.condition_fingerprint,
            "application": self.application,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "action": self.action,
            "catalog_generation": self.catalog_generation,
            "itil_change_id": self.itil_change_id,
            "cmdb_ci_id": self.cmdb_ci_id,
            "authorization_ref": self.authorization_ref,
        }

    @model_validator(mode="after")
    def _derive_or_verify_id(self) -> "ActionIntent":
        expected = stable_intent_id(self.identity())
        if self.intent_id and self.intent_id != expected:
            raise ValueError("intent_id does not match the stable intent identity")
        if not self.intent_id:
            object.__setattr__(self, "intent_id", expected)
        return self


class ActionEvent(BaseModel):
    """One hash-chained append-only lifecycle transition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_id: str = SCHEMA
    intent_id: str
    sequence: int = Field(ge=0)
    state: ActionState
    occurred_at: datetime
    actor: str = Field(min_length=1, max_length=128)
    evidence_ref: str | None = Field(default=None, max_length=1024)
    detail: dict[str, Any] = Field(default_factory=dict)
    previous_hash: str | None = None
    signature_suite: str | None = None
    signature: str | None = None
    event_hash: str


class ActionLedger:
    """Filesystem-backed intent cores and append-only lifecycle event streams."""

    def __init__(
        self,
        root: str | Path,
        *,
        signer: Callable[[bytes], str] | None = None,
        verifier: Callable[[bytes, str], bool] | None = None,
        require_signatures: bool = False,
    ) -> None:
        self.root = Path(root)
        self.intents_dir = self.root / "intents"
        self.events_dir = self.root / "events"
        self.lock_path = self.root / ".lock"
        self.signer = signer
        self.verifier = verifier
        self.require_signatures = require_signatures
        if require_signatures and (signer is None or verifier is None):
            raise ValueError("signed action ledger requires signer and verifier")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @staticmethod
    def _validate_id(intent_id: str) -> None:
        if not _ID_RE.fullmatch(intent_id):
            raise ValueError("invalid action intent id")

    def _intent_path(self, intent_id: str) -> Path:
        self._validate_id(intent_id)
        return self.intents_dir / f"{intent_id}.json"

    def _event_path(self, intent_id: str) -> Path:
        self._validate_id(intent_id)
        return self.events_dir / f"{intent_id}.jsonl"

    @staticmethod
    def _event_hash(event: ActionEvent) -> str:
        material = event.model_dump(mode="json", exclude={"event_hash"})
        return hashlib.sha256(_canonical(material)).hexdigest()

    @staticmethod
    def _signature_material(payload: dict[str, Any]) -> bytes:
        """Return the event bytes bound to the actor and authorization reference."""
        normalized = ActionEvent.model_validate(
            {**payload, "event_hash": payload.get("event_hash") or "pending"}
        ).model_dump(mode="json")
        return _canonical(
            {k: v for k, v in normalized.items() if k not in {"event_hash", "signature"}}
        )

    def create(
        self,
        intent: ActionIntent,
        *,
        actor: str,
        evidence_ref: str | None = None,
    ) -> ActionEvent:
        """Create an immutable intent core and its initial ``observed`` event.

        Recreating the same intent is idempotent. A stable-ID collision with
        different content is rejected.
        """
        with self._locked():
            path = self._intent_path(intent.intent_id)
            if path.exists():
                current = ActionIntent.model_validate_json(path.read_text(encoding="utf-8"))
                if current.identity() != intent.identity():
                    raise ValueError(f"stable intent collision: {intent.intent_id}")
                return self._read_events_unlocked(intent.intent_id)[0]
            self.intents_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.events_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write_text(path, intent.model_dump_json(indent=2) + "\n")
            return self._append_unlocked(
                intent.intent_id,
                ActionState.OBSERVED,
                occurred_at=intent.created_at,
                actor=actor,
                evidence_ref=evidence_ref,
                detail={},
            )

    def append(
        self,
        intent_id: str,
        state: ActionState,
        *,
        occurred_at: datetime,
        actor: str,
        evidence_ref: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> ActionEvent:
        """Append a valid next lifecycle state and return the event."""
        with self._locked():
            if not self._intent_path(intent_id).is_file():
                raise ValueError(f"unknown action intent: {intent_id}")
            return self._append_unlocked(
                intent_id,
                state,
                occurred_at=occurred_at,
                actor=actor,
                evidence_ref=evidence_ref,
                detail=detail or {},
            )

    def _append_unlocked(
        self,
        intent_id: str,
        state: ActionState,
        *,
        occurred_at: datetime,
        actor: str,
        evidence_ref: str | None,
        detail: dict[str, Any],
    ) -> ActionEvent:
        events = self._read_events_unlocked(intent_id)
        if events:
            current = events[-1].state
            if state not in _TRANSITIONS[current]:
                raise ValueError(f"invalid action transition: {current.value} -> {state.value}")
        elif state is not ActionState.OBSERVED:
            raise ValueError("first action state must be observed")
        payload: dict[str, Any] = {
            "schema_id": SCHEMA,
            "intent_id": intent_id,
            "sequence": len(events),
            "state": state.value,
            "occurred_at": occurred_at.isoformat(),
            "actor": actor,
            "evidence_ref": evidence_ref,
            "detail": detail,
            "previous_hash": events[-1].event_hash if events else None,
            "signature_suite": "capauth-pgp-v1" if self.signer is not None else None,
            "signature": None,
        }
        if self.signer is not None:
            payload["signature"] = self.signer(self._signature_material(payload))
        payload["event_hash"] = "pending"
        provisional = ActionEvent.model_validate(payload)
        payload["event_hash"] = self._event_hash(provisional)
        event = ActionEvent.model_validate(payload)
        path = self._event_path(intent_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(event.model_dump_json() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return event

    def read_intent(self, intent_id: str) -> ActionIntent:
        """Read and validate an immutable intent core."""
        text = self._intent_path(intent_id).read_text(encoding="utf-8")
        return ActionIntent.model_validate_json(text)

    def events(self, intent_id: str) -> list[ActionEvent]:
        """Read and verify the complete hash-chained event stream."""
        with self._locked():
            return self._read_events_unlocked(intent_id)

    def _read_events_unlocked(self, intent_id: str) -> list[ActionEvent]:
        path = self._event_path(intent_id)
        if not path.exists():
            return []
        events: list[ActionEvent] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                raw = json.loads(line)
                event = ActionEvent.model_validate(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"invalid action event at line {line_number}") from exc
            if event.sequence != len(events):
                raise ValueError(f"non-contiguous action sequence at line {line_number}")
            expected_previous = events[-1].event_hash if events else None
            if event.previous_hash != expected_previous or event.event_hash != self._event_hash(
                event
            ):
                raise ValueError(f"broken action event hash chain at line {line_number}")
            if event.signature is None:
                if self.require_signatures:
                    raise ValueError(f"unsigned action event at line {line_number}")
            elif self.verifier is None:
                if self.require_signatures:
                    raise ValueError(f"action event verifier unavailable at line {line_number}")
            elif not self.verifier(self._signature_material(raw), event.signature):
                raise ValueError(f"invalid action event signature at line {line_number}")
            events.append(event)
        return events

    def current_state(self, intent_id: str) -> ActionState:
        """Return the folded current lifecycle state."""
        events = self.events(intent_id)
        if not events:
            raise ValueError(f"action intent has no events: {intent_id}")
        return events[-1].state
