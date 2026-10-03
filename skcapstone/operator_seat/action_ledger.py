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

#: A lineage in one of these states is DONE: it will never accept another
#: transition (see ``_TRANSITIONS`` above, all three map to an empty set).
#: Derived, not hand-maintained, so the two can never drift apart.
_TERMINAL_STATES: frozenset[ActionState] = frozenset(
    state for state, nxt in _TRANSITIONS.items() if not nxt
)


def _canonical(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def stable_intent_id(identity: dict[str, Any]) -> str:
    """Derive a stable, non-secret identifier from an intent identity."""
    return "ai-" + hashlib.sha256(_canonical(identity)).hexdigest()[:24]


def _identity_with_occurrence(base_identity: dict[str, Any], occurrence: int) -> dict[str, Any]:
    """Fold ``occurrence`` into a base (9-field) identity dict.

    ``occurrence`` is omitted entirely when it is 0 (the overwhelming common
    case: a condition's first-ever, still-open, or currently-active lineage),
    so the identity -- and therefore ``stable_intent_id`` -- is BYTE IDENTICAL
    to the pre-occurrence hash for every existing caller that never sees a
    terminal-then-recurring condition. Only a genuinely later occurrence
    (``occurrence >= 1``, see ``ActionLedger.resolve_occurrence``) changes the
    hash at all.
    """
    if not occurrence:
        return dict(base_identity)
    return {**base_identity, "occurrence": occurrence}


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
    occurrence: int = Field(default=0, ge=0)
    #: The literal observed-condition type name (e.g. "BridgeAlive"), NOT part of
    #: dedup identity (``identity()`` below never reads it, same treatment as
    #: ``verification``/``rollback``): a dispatcher acting later needs a way to
    #: re-observe and verify the SAME bound condition without ever reading the
    #: original proposal/brief (the actuator's closed input set,
    #: AUTONOMY_ARCHITECTURE.md section 3.2). ``condition_fingerprint`` alone is
    #: a one-way hash and cannot serve that purpose. Optional/default-None so
    #: every pre-existing serialized intent keeps parsing unchanged.
    condition_type: str | None = Field(default=None, max_length=128)

    @field_validator("schema_id")
    @classmethod
    def _known_schema(cls, value: str) -> str:
        if value != SCHEMA:
            raise ValueError(f"unsupported action intent schema: {value}")
        return value

    def identity(self) -> dict[str, Any]:
        """Return the fields defining deduplication identity.

        ``created_at`` is deliberately excluded (see module docstring history):
        identity must be a pure function of WHAT is being done, not WHEN, so
        that the same real-world condition observed repeatedly within one
        still-open episode dedupes to one intent. ``occurrence`` is the one
        exception, and it is not a clock: it is assigned by
        ``ActionLedger.resolve_occurrence`` only after a PRIOR lineage for
        this exact identity has already reached a terminal state
        (VERIFIED/ROLLED_BACK/ESCALATED), so it distinguishes "this condition,
        genuinely recurring after its last lineage concluded" from "this
        condition, observed again while still being worked." See that method
        for the full derivation.
        """
        base = {
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
        return _identity_with_occurrence(base, self.occurrence)

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

    def resolve_occurrence(self, base_identity: dict[str, Any]) -> int:
        """Return the occurrence number to bind for a real-world action identity.

        ``base_identity`` is the same 9-field dict :meth:`ActionIntent.identity`
        produces for ``occurrence=0`` (condition_fingerprint, application,
        target_kind, target_id, action, catalog_generation, itil_change_id,
        cmdb_ci_id, authorization_ref). This walks occurrence 0, 1, 2, ...
        deterministically -- no clock, no external counter, purely a function
        of what is already durably on disk in THIS ledger:

        - an occurrence whose intent file does not exist yet is unused: return
          it (the first-ever occurrence, or the next fresh one once every
          prior occurrence has concluded).
        - an occurrence whose lineage is NOT yet in a terminal state is the
          SAME still-open episode: return it, so the caller's ``create()``
          call is idempotent and no second lifecycle is started for a
          condition that is still being worked (dedup holds).
        - an occurrence whose lineage IS terminal (VERIFIED / ROLLED_BACK /
          ESCALATED) has concluded and can never accept another transition
          (see ``_TERMINAL_STATES``): it cannot represent a new real-world
          firing, so probe the next occurrence.

        Callers MUST memoize the result for the lifetime of one caller-defined
        "episode" (in ``operator_seat.loop``, one operator pass) so that two
        proposals for the same identity observed within that SAME episode
        resolve to the SAME occurrence even if the first one's lineage
        reaches a terminal state (e.g. actuates and is VERIFIED) before the
        second is examined. Only a genuinely later episode, calling this
        again with a fresh memo, is allowed to see the prior terminal state
        and advance the occurrence. Called fresh every time (no memo) this
        method is safe but degenerate: it would treat every duplicate within
        an episode as a new occurrence once the first completes, which is
        exactly the failure mode ``loop.py``'s per-pass cache exists to avoid.
        """
        with self._locked():
            return self._resolve_occurrence_unlocked(base_identity)

    def _resolve_occurrence_unlocked(self, base_identity: dict[str, Any]) -> int:
        occurrence = 0
        while True:
            candidate_id = stable_intent_id(_identity_with_occurrence(base_identity, occurrence))
            if not self._intent_path(candidate_id).is_file():
                return occurrence
            events = self._read_events_unlocked(candidate_id)
            if not events or events[-1].state not in _TERMINAL_STATES:
                return occurrence
            occurrence += 1

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

    def list_intents(self, *, state: ActionState | None = None) -> list[str]:
        """Return every intent id on disk, optionally filtered to one folded state.

        This is the ledger's QUEUE face (AUTONOMY_ARCHITECTURE.md section 3.2):
        an intent whose current folded state is PROPOSED is durable evidence
        that "something wants to happen", and this is how a dispatcher finds
        those without ever needing to read the proposals/brief that created
        them. Sorted for a deterministic iteration order; a corrupt or
        unreadable event stream for one intent is skipped rather than raised,
        so one bad file cannot hide every other pending intent from the
        dispatcher (same fail-contained posture ``loop.py`` already uses per
        proposal).
        """
        with self._locked():
            if not self.intents_dir.is_dir():
                return []
            ids = sorted(p.stem for p in self.intents_dir.glob("ai-*.json"))
            if state is None:
                return ids
            matching = []
            for intent_id in ids:
                try:
                    events = self._read_events_unlocked(intent_id)
                except ValueError:
                    continue
                if events and events[-1].state is state:
                    matching.append(intent_id)
            return matching
