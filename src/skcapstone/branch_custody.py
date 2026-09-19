"""Fail-closed custody leases for candidate pull-request branches.

The lease is an auditable CardStore event stream, not a filesystem lock.  A
mutation is authorized only when the lease owner, generation, and expected
remote head still match.  Review activation is a separate event and freezes
all mutations until the review is explicitly completed.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Any

from skcoord.card_store import CardStore, card_mutation_lock

_SHA = re.compile(r"^[0-9a-f]{40}$", re.I)


class CustodyError(RuntimeError):
    """A custody invariant rejected an operation."""


@dataclass(frozen=True)
class LeaseReceipt:
    repository: str
    branch: str
    card: str
    agent: str
    session: str
    expected_head: str
    generation: int
    acquired_at: float
    expires_at: float
    receipt: str

    def as_dict(self) -> dict[str, Any]:
        return {"repository": self.repository, "branch": self.branch, "card": self.card,
                "agent": self.agent, "session": self.session, "expected_head": self.expected_head,
                "generation": self.generation, "acquired_at": self.acquired_at,
                "expires_at": self.expires_at, "receipt": self.receipt}


class BranchCustody:
    """Coordinate one card's repository/branch leases through CardStore events."""

    def __init__(self, store: CardStore, card_id: str, *, clock: Callable[[], float] = time.time):
        self.store, self.card_id, self.clock = store, card_id, clock

    def _events(self) -> list[dict[str, Any]]:
        # CardStore owns parsing, validation, and append-only durability.
        return list(self.store._read_events(self.card_id))

    @staticmethod
    def _key(repository: str, branch: str) -> str:
        return hashlib.sha256((repository + "\0" + branch).encode()).hexdigest()

    def _state(self, repository: str, branch: str) -> tuple[dict[str, Any] | None, bool]:
        key = self._key(repository, branch)
        lease = None
        frozen = False
        for event in self._events():
            payload = event.get("payload", event)
            if payload.get("custody_key") != key:
                continue
            action = event.get("action")
            if action == "branch_lease_acquired":
                lease = dict(payload)
            elif action == "branch_lease_released":
                lease = None
            elif action == "branch_review_activated":
                frozen = True
            elif action == "branch_review_completed":
                frozen = False
            elif action == "branch_lease_head_advanced":
                if lease and lease.get("receipt") == payload.get("receipt"):
                    lease["expected_head"] = payload.get("expected_head")
            elif action == "branch_lease_recovered":
                lease = dict(payload)
        return lease, frozen

    def _append(self, action: str, **payload: Any) -> None:
        # append_event serializes the payload and validates the resulting line.
        self.store.append_event(self.card_id, action, "branch-custody", **payload)

    def acquire(self, repository: str, branch: str, expected_head: str, agent: str,
                session: str, *, ttl: float = 900) -> LeaseReceipt:
        if not repository or not branch or not agent or not session or not _SHA.fullmatch(expected_head):
            raise CustodyError("repository, branch, owner, session, and a 40-hex head are required")
        if ttl <= 0 or ttl > 86400:
            raise CustodyError("lease expiry is outside the bounded range")
        with card_mutation_lock(self.store.home, self.card_id):
            current, frozen = self._state(repository, branch)
            now = self.clock()
            if frozen:
                raise CustodyError("candidate branch is frozen for exact-head review")
            if current and current["expires_at"] > now:
                raise CustodyError("branch already has a live mutation lease")
            generation = int(current["generation"]) + 1 if current else 1
            expires = now + ttl
            receipt_data = {"card": self.card_id, "custody_key": self._key(repository, branch),
                            "generation": generation, "session": session, "expected_head": expected_head,
                            "acquired_at": now, "expires_at": expires}
            token = hashlib.sha256(json.dumps(receipt_data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            payload = {**receipt_data, "repository": repository, "branch": branch, "agent": agent, "receipt": token}
            self._append("branch_lease_acquired", **payload)
            return LeaseReceipt(repository, branch, self.card_id, agent, session, expected_head,
                                generation, now, expires, token)

    def authorize(self, receipt: LeaseReceipt, remote_head: str, *, operation: str) -> None:
        if not _SHA.fullmatch(remote_head) or not operation:
            raise CustodyError("invalid mutation authorization")
        with card_mutation_lock(self.store.home, self.card_id):
            lease, frozen = self._state(receipt.repository, receipt.branch)
            if frozen:
                raise CustodyError("review freeze blocks branch mutation")
            if (not lease or lease.get("receipt") != receipt.receipt
                    or lease.get("session") != receipt.session or lease.get("agent") != receipt.agent
                    or lease.get("generation") != receipt.generation):
                raise CustodyError("lease owner or generation changed")
            if lease.get("expected_head") != remote_head or lease["expires_at"] <= self.clock():
                raise CustodyError("stale expected head or expired lease")

    def update_head(self, receipt: LeaseReceipt, new_head: str, *, remote_head: str, operation: str) -> None:
        self.authorize(receipt, remote_head, operation=operation)
        if not _SHA.fullmatch(new_head):
            raise CustodyError("invalid new head")
        with card_mutation_lock(self.store.home, self.card_id):
            lease, _ = self._state(receipt.repository, receipt.branch)
            if not lease or lease.get("receipt") != receipt.receipt or lease.get("expected_head") != remote_head:
                raise CustodyError("compare-and-swap failed")
            self._append("branch_lease_head_advanced", custody_key=self._key(receipt.repository, receipt.branch),
                         receipt=receipt.receipt, generation=receipt.generation, expected_head=new_head,
                         operation=operation)

    def activate_review(self, receipt: LeaseReceipt, review_id: str) -> None:
        self.authorize(receipt, receipt.expected_head, operation="review_activate")
        with card_mutation_lock(self.store.home, self.card_id):
            self._append("branch_review_activated", custody_key=self._key(receipt.repository, receipt.branch),
                         receipt=receipt.receipt, generation=receipt.generation, head=receipt.expected_head,
                         review_id=review_id)

    def complete_review(self, repository: str, branch: str, review_id: str) -> None:
        with card_mutation_lock(self.store.home, self.card_id):
            self._append("branch_review_completed", custody_key=self._key(repository, branch), review_id=review_id)

    def release(self, receipt: LeaseReceipt) -> None:
        with card_mutation_lock(self.store.home, self.card_id):
            lease, _ = self._state(receipt.repository, receipt.branch)
            if not lease or lease.get("receipt") != receipt.receipt:
                raise CustodyError("lease is not owned by this receipt")
            self._append("branch_lease_released", custody_key=self._key(receipt.repository, receipt.branch),
                         receipt=receipt.receipt, generation=receipt.generation)

    def recover(self, repository: str, branch: str, *, expected_head: str, agent: str,
                session: str, owner_live: bool, divergent_work: str) -> LeaseReceipt:
        if owner_live:
            raise CustodyError("cannot recover a live lease")
        if not divergent_work or not _SHA.fullmatch(expected_head):
            raise CustodyError("recovery must preserve divergent or uncommitted work custody")
        with card_mutation_lock(self.store.home, self.card_id):
            current, frozen = self._state(repository, branch)
            if frozen or (current and current["expires_at"] > self.clock()):
                raise CustodyError("lease is live or review-frozen")
            receipt = self.acquire(repository, branch, expected_head, agent, session)
            self._append("branch_lease_recovered", custody_key=self._key(repository, branch),
                         prior_lease=current or {}, divergent_work=divergent_work, **receipt.as_dict())
            return receipt
