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
    """Unix seconds, or 0.0 for anything unparseable.

    A timestamp with no offset is resolved as UTC rather than host-local.
    Every writer in this store stamps tz-aware ISO-8601, but ``append_event``
    merges its payload over the envelope, so a caller passing ``ts=`` can
    land a naive value. Resolving that host-local would shift apparent
    idleness by the host's UTC offset, which is a 9 hour error on an Asian
    host and a 14 hour one at UTC+14: enough to reclaim a live worker's card
    or to hide a dead one.
    """
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _is_worker_for(writer: str, card_id: str) -> bool:
    """Is this writer a worker acting on THIS card, rather than a seat?

    Worker identities embed the card id by construction
    (``pi-codex-chiap08-0f7b2e6c``, ``kimi-chiap01-34115541``,
    ``codex-02963e5f-r4``), and seat identities never do. That single test
    is what separates "a worker for this card is alive" from "a seat filed
    an observation about an abandoned card", and it needs no list of seat
    names: a card id is eight hex characters, so no seat name can contain
    one. An explicit seat blocklist was written first and then removed,
    because a mutation check proved it never fired.

    It is deliberately CONSERVATIVE. A sibling whose identity does not
    reference the card (``cursor-w73-live`` writing on card ``73c201a1``) is
    not counted, so such a card can expire while that writer is active.
    Phase 2 of the rollout exists to measure exactly this before anything
    is enforced.

    This matters because the owning identity is frequently NOT the working
    identity for the same card. Measured on chi, card 0f7b2e6c: the owner
    ``pi-codex-chiap02-0f7b2e6c`` wrote one event, its sibling
    ``pi-codex-chiap08-0f7b2e6c`` wrote four. Counting only the owner's own
    writes would reclaim that card while the work was in progress.
    """
    if not writer or not card_id:
        return False
    return card_id in writer


def _evidence_activity(root: Path) -> dict[str, dict[str, float]]:
    """card_id -> writer -> that writer's latest timestamp, from card_events.

    ``<sovereign home>/coordination/card_events/*.jsonl`` is a SECOND event
    store, and it is where the review workflow actually lands: `coord link`,
    `verdict` and `evidence` are written here through ``CardEventLog`` and
    never touch ``cards/<id>/events/``. Measured on chi it holds 81,207
    records (68,666 link, 7,799 move, 701 verdict) against roughly 13,000 in
    the per-card shards.

    Reading only the per-card shards therefore made the idle clock blind to
    the evidence store, and a worker posting evidence hourly and a verdict
    seconds before the sweep still measured as idle for its whole run. That
    was demonstrated end to end against the real write paths: the card
    folded to owner=None while the worker was alive and working.

    Read in ONE pass and indexed, rather than re-read per card, because this
    runs inside a 5 minute dispatcher cycle.
    """
    index: dict[str, dict[str, float]] = {}
    events_dir = root / "coordination" / "card_events"
    if not events_dir.is_dir():
        return index
    try:
        names = sorted(events_dir.iterdir())
    except OSError:
        return index
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
                card_id = event.get("card_id")
                writer = event.get("writer")
                if not card_id or not writer:
                    continue
                stamp = _parse_ts(event.get("ts"))
                if not stamp:
                    continue
                per_card = index.setdefault(str(card_id), {})
                writer = str(writer)
                if stamp > per_card.get(writer, 0.0):
                    per_card[writer] = stamp

                # A worker_liveness link is the DISPATCHER recording that it
                # just observed this worker alive, so its writer is
                # skfleet-rotate and the OWNER's name is in link_value, as
                # "<owner>|<claim_revision>". Attributing it to the owner is
                # what makes it usable as liveness.
                #
                # This is the strongest liveness signal the estate produces:
                # measured on chi, it is emitted every 2 to 3 minutes per live
                # worker, and 133 of 133 evidence events on a sample of live
                # cards were of exactly this kind, with the workers themselves
                # writing nothing at all. Ignoring it would age out a card
                # whose worker the dispatcher was actively watching.
                if event.get("link_key") == "worker_liveness":
                    subject = str(event.get("link_value") or "").split("|", 1)[0].strip()
                    if subject and stamp > per_card.get(subject, 0.0):
                        per_card[subject] = stamp
    return index


def _last_activity_at(
    root: Path,
    card_id: str,
    owner: str,
    evidence: dict[str, dict[str, float]],
) -> float:
    """When work on this card was last evidenced, across BOTH event stores.

    Counts an event when it was written by the owner, or by a worker
    identity that references this card. Excludes seat writers, so a seat
    filing observations on an abandoned card does not keep its claim alive.
    """
    latest = 0.0

    # Store 1: the per-card structural shards.
    events_dir = root / "cards" / str(card_id) / "events"
    if events_dir.is_dir():
        try:
            names = sorted(events_dir.iterdir())
        except OSError:
            names = []
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
                    writer = str(event.get("writer") or "")
                    counts = (
                        writer == owner
                        or event.get("owner") == owner
                        or _is_worker_for(writer, str(card_id))
                    )
                    if not counts:
                        continue
                    stamp = _parse_ts(event.get("ts"))
                    if stamp > latest:
                        latest = stamp

    # Store 2: the evidence overlay (link, verdict, evidence, move).
    for writer, stamp in (evidence.get(str(card_id)) or {}).items():
        if writer == owner or _is_worker_for(writer, str(card_id)):
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
    evidence = _evidence_activity(root)

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
                last_owner_event_at=_last_activity_at(root, card_id, str(owner), evidence),
            )
        )
    return out
