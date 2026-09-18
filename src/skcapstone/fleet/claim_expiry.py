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
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

#: Hours of owner inactivity before a claim may be reclaimed. Deliberately
#: generous: the failure is asymmetric. Too long leaves a card stuck, which
#: is the status quo; too short steals a card from a worker mid-run.
DEFAULT_TTL_HOURS = 48.0

#: The shortest TTL an operator may configure. The deadline exists to
#: tolerate a live worker that has not written an event recently, so a
#: margin measured in minutes defeats its purpose.
MIN_TTL_HOURS = 1.0

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
    """The configured TTL in seconds, falling back to the default.

    Every rejection path returns the DEFAULT rather than raising, because
    this is read inside a dispatcher cycle and a hard failure there costs
    more than an unexpectedly conservative deadline.

    Two rejections matter more than they look:

    ``nan`` parses as a float and survives a ``<= 0`` test, because every
    comparison against nan is False. It then makes ``idle <= ttl`` False for
    ANY claim, so every claim in the store becomes reclaimable, including
    one made a minute ago. Verified before this guard existed.

    A TTL below MIN_TTL_HOURS is refused as well. The deadline is a safety
    margin against a live worker that has simply not written an event
    recently, and a sub-hour margin is not one. ``SKFLEET_CLAIM_TTL_H=.5``
    would otherwise arm a 30-minute deadline.
    """
    raw = str(env.get("SKFLEET_CLAIM_TTL_H", "")).strip()
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TTL_HOURS * 3600.0
    if not math.isfinite(hours) or hours < MIN_TTL_HOURS:
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


def _last_owner_event_at(root: Path, card_id: str, owner: str) -> float:
    """When the owner last wrote ANY event to the card it holds.

    This is the only thing the event log is read for. Ownership itself comes
    from the fold, which is the authority, so nothing here needs to know how
    a claim is won or lost.

    Events are sharded one file per writer (``jarvis@chiap03.jsonl``,
    ``pi-codex-chiap01-<cid>@chiap01.jsonl``), so every shard has to be read;
    the owner's own shard is not the only place its name appears.
    """
    events_dir = root / "cards" / str(card_id) / "events"
    if not events_dir.is_dir():
        return 0.0
    latest = 0.0
    try:
        names = sorted(events_dir.iterdir())
    except OSError:
        return 0.0
    for name in names:
        if name.suffix != ".jsonl":
            continue
        try:
            handle = name.open(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("writer") != owner and event.get("owner") != owner:
                    continue
                stamp = _parse_ts(event.get("ts"))
                if stamp > latest:
                    latest = stamp
    return latest


def observe(home: Path | str) -> list[ClaimObservation]:
    """Every claim the CardStore fold reports as held, with owner idleness.

    Ownership and the claim revision come from ``CardStore.fold()`` rather
    than from a local replay of the event log. An earlier version of this
    function replayed the events itself and it was wrong four separate
    times, each found only by diffing against the fold on a live store:

      1. it sorted by ``(seq, ts)``, but events shard one file per writer and
         ``seq`` restarts at 0 in each, so releases were lost (98 cards)
      2. it ignored ``assign`` while honoring ``unassign`` (3 cards)
      3. fixing (2) let a claim resurrect a voided card (2 cards), which is
         the documented regression that once left 88 of 114 voids ineffective
      4. it took ``claim_revision`` literally, where the fold falls back to
         ``event_id``, and it let a second claim by a DIFFERENT owner win,
         where the fold refuses it and keeps the first owner

    (4) is the one that settles the argument. The fold's second-claim rule is
    status-dependent (it refuses only while the card is in ready, doing or
    review), so reproducing it faithfully means reproducing column
    transitions too, which means reimplementing the fold. Two
    implementations of one rule drift, and the drift is silent. So this
    function asks the fold and reads the log only for a timestamp the fold
    does not retain.
    """
    from skcoord.card_store import CardStore

    root = Path(home)
    cards = CardStore(root).list_cards(include_archived=False, degrade_unreadable=True)

    out: list[ClaimObservation] = []
    for card in cards:
        owner = getattr(card, "owner", None)
        if not owner:
            continue
        if getattr(card, "archived", False):
            continue
        card_id = str(getattr(card, "id", "") or "")
        if not card_id:
            continue
        meta = getattr(card, "meta", None) or {}
        revision = meta.get("_claim_revision") or None
        out.append(
            ClaimObservation(
                card_id=card_id,
                owner=str(owner),
                claim_revision=str(revision) if revision else None,
                last_owner_event_at=_last_owner_event_at(root, card_id, str(owner)),
            )
        )
    return out
