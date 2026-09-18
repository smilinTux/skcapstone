"""Decide which card claims are past their idle deadline.

Pure: no I/O, no clock, no environment beyond an explicitly passed mapping.
Every refusal is named in ``ExpiryVerdict.reason`` so the reaper's report
mode can explain itself without re-deriving anything.

The deadline is measured from the last event the OWNER wrote on the card it
holds, not from the claim timestamp. A worker doing real work writes move,
describe, evidence and verdict events continuously; one that has written
nothing for the whole TTL is dead or making no progress, and in both cases
the card should return to the pool. Measured on chi 2026-09-18, the 349
held claims have a minimum owner-idle of 30.9 hours, so a 48 hour deadline
separates them from live work with room to spare.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

#: Hours of owner inactivity before a claim may be reclaimed. Deliberately
#: generous: the failure is asymmetric. Too long leaves a card stuck, which
#: is the status quo; too short steals a card from a worker mid-run.
DEFAULT_TTL_HOURS = 48.0

_MODES = ("off", "report", "enforce")


@dataclass(frozen=True)
class ClaimObservation:
    """One held claim, as read from the CardStore fold plus its event log."""

    card_id: str
    owner: str
    claim_revision: str | None
    last_owner_event_at: float


@dataclass(frozen=True)
class ExpiryVerdict:
    card_id: str
    owner: str
    claim_revision: str
    idle_seconds: float
    reclaimable: bool
    reason: str


def ttl_seconds_from_env(env: Mapping[str, str]) -> float:
    raw = str(env.get("SKFLEET_CLAIM_TTL_H", "")).strip()
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TTL_HOURS * 3600.0
    if hours <= 0:
        return DEFAULT_TTL_HOURS * 3600.0
    return hours * 3600.0


def mode_from_env(env: Mapping[str, str]) -> str:
    raw = str(env.get("SKFLEET_CLAIM_TTL_MODE", "")).strip().lower()
    return raw if raw in _MODES else "off"


def evaluate(
    observations: Iterable[ClaimObservation], *, now: float, ttl_seconds: float
) -> list[ExpiryVerdict]:
    """Verdict per observation. Never raises on malformed input."""
    out: list[ExpiryVerdict] = []
    for o in observations:
        idle = now - float(o.last_owner_event_at or 0.0)
        reason = ""
        ok = False
        if not o.claim_revision:
            reason = "no-claim-revision"
        elif not o.last_owner_event_at:
            reason = "no-owner-activity"
        elif o.last_owner_event_at > now:
            reason = "future-timestamp"
        elif idle <= ttl_seconds:
            reason = "within-ttl"
        else:
            reason = "idle-beyond-ttl"
            ok = True
        out.append(
            ExpiryVerdict(
                card_id=o.card_id,
                owner=o.owner,
                claim_revision=o.claim_revision or "",
                idle_seconds=idle,
                reclaimable=ok,
                reason=reason,
            )
        )
    return out
