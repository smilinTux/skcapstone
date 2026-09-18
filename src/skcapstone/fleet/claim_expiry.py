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

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

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


def _parse_ts(value: object) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


#: Void and archive are FINAL. A claim that arrives after them does not
#: resurrect the card, and treating it as if it did is a known, costly
#: regression: 88 of 114 voids were once silently ineffective and 88 cards
#: stayed resurrectable, which reversed decisions the operator had already
#: made. The fold enforces this, so the replay must too. Measured on chi:
#: card 7e2c6788 took a claim 21 SECONDS after its void+archive, and
#: a80f87a9 took one an hour after, and the fold reports neither as held.
_TERMINAL_FINAL = {"void", "archive"}

#: Complete is SOFT: it clears ownership, but a later assign or claim
#: legitimately reopens the card, and the fold honors that. Card cec6b1c0
#: on chi is the worked example (claim, release, claim, complete, complete,
#: assign) and the fold reports it held by `pi`.
_TERMINAL_SOFT = {"complete"}
#: Actions that SET an owner. `claim` is the claim-specific primitive and
#: carries a claim_revision; `assign` is the generic assignment primitive
#: and carries none. Both set `card.owner` in the fold, so a replay that
#: honors `unassign` while ignoring `assign` is asymmetric and
#: under-reports held cards.
_ACQUIRE = {"claim", "assign"}
_RELEASE = {"release_claim", "unassign"}


def observe(home: Path) -> list[ClaimObservation]:
    """Every claim the store currently reports as held, with owner idleness.

    Reads the event log directly rather than the CardStore fold, because the
    deadline needs the last event the OWNER wrote, which the fold does not
    retain. Ownership itself is decided by replaying the claim/release pairs
    in sequence order, which is what the fold does: counting claim events
    against release events overcounts, since a worker re-claiming a card it
    already holds writes a second claim that one release settles.
    """
    root = Path(home) / "cards"
    if not root.is_dir():
        return []

    out: list[ClaimObservation] = []
    for card_dir in sorted(root.iterdir()):
        events_dir = card_dir / "events"
        if not events_dir.is_dir():
            continue
        events: list[dict] = []
        try:
            names = sorted(events_dir.iterdir())
        except OSError:
            continue
        for fn in names:
            try:
                fh = fn.open(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            with fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict):
                        events.append(e)
        if not events:
            continue
        # Order by TIMESTAMP first, seq only as a tiebreak. Events are sharded
        # one file per writer (`jarvis@chiap03.jsonl`,
        # `pi-codex-chiap01-<cid>@chiap01.jsonl`), and `seq` restarts at 0 in
        # EVERY file, so seq is meaningless across writers. Sorting by seq
        # first interleaves writers and can place a release before a later
        # claim by a different writer, leaving the card looking held forever.
        # Card bf80259a is the worked example: a claim at seq=1 06:18:40 and a
        # release at seq=0 06:27:06, where seq-first ordering loses the
        # release and disagrees with CardStore.fold().
        events.sort(key=lambda e: (_parse_ts(e.get("ts")), e.get("seq") or 0))

        owner: str | None = None
        revision: str | None = None
        terminal = False
        for e in events:
            action = e.get("action")
            if action in _TERMINAL_FINAL:
                # Final: stop here. Nothing after a void or an archive can
                # put this card back in play.
                terminal = True
                owner = None
                revision = None
                break
            if action in _TERMINAL_SOFT:
                # Soft: clears ownership without ending the replay, because
                # a later assign or claim reopens the card.
                terminal = True
                owner = None
                revision = None
            elif action in _ACQUIRE and e.get("owner"):
                owner = str(e["owner"])
                # Only `claim` carries a claim_revision. An `assign` sets an
                # owner with no revision, and therefore no CAS fence, so
                # evaluate() will refuse to reclaim it ("no-claim-revision").
                # That refusal is correct: releasing an assignment needs
                # `coord unassign`, not `release-claim` with an expected
                # revision. Recording it still matters, because the report
                # has to make such a card VISIBLE rather than pretend the
                # store holds nothing.
                revision = e.get("claim_revision") or None
                terminal = False
            elif action in _RELEASE:
                owner = None
                revision = None
        if terminal or not owner:
            continue

        last_owner = 0.0
        for e in events:
            if e.get("writer") == owner or e.get("owner") == owner:
                t = _parse_ts(e.get("ts"))
                if t > last_owner:
                    last_owner = t
        out.append(
            ClaimObservation(
                card_id=card_dir.name,
                owner=owner,
                claim_revision=revision,
                last_owner_event_at=last_owner,
            )
        )
    return out
