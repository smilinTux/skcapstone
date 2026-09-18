"""Refuse a claim on a card that churns, until someone records WHY it is stuck.

The claim ceiling in skfleet-rotate stops dispatch only AFTER five wasted
claims, and it never asks why. This asks at the claim itself.

MEASURED ON THE LIVE CHI CLUSTER, 2026-09-18:

    83e498b6   58 claims, 6 owners, 0 completions. 47 of the 58 are ONE seat
               re-claiming every 10 minutes. It needs the operator's Tailscale
               admin console, so no agent can ever finish it.
    bfbb2986   55 claims, 46 from one looping seat. It needs host chiwk12,
               which is unreachable. Already labelled not-claimable, and
               refilled anyway by pool-refill waves.

    112        distinct cards now permanently excluded by the claim ceiling,
               against a dispatchable pool of roughly 24 to 30 ready cards.

Every one of those cycles burned a worker slot, a dispatch and a model-call
budget, and returned the card exactly where it was.

THE RULE. A card that has accumulated enough claim attempts, or enough
distinct claim owners, and has never reached a terminal state, may not be
claimed again until it carries a recorded blocker. Recording the blocker is
what re-permits it. Nothing is labelled, nothing is voided, nothing is
retried: the card waits for one sentence that says what is in the way.

NO NEW VOCABULARY. The blocker this looks for is the one the estate already
writes, validated by ``blocked_verdict`` and parsed by ``review_admission``:

    BLOCKED. blocked_on=human referent=approval:tailscale-admin-console

This estate has eight recorded cases of a mechanism fully implemented, tested
and called by nothing, plus ``exit_gates`` which exists on CardCore and on
every host and is used by 0 of 7,154 cards. A ninth label would be a ninth
such case. So the breaker reads the existing field, requires the existing
shape, and its unblock path is a verdict workers are already told to write.

DEFAULT OFF. ``SKFLEET_CHURN_BREAKER_MODE`` is off/report/enforce, exactly
like ``SKFLEET_CLAIM_TTL_MODE``, and off returns before doing any work.
Refusing a claim removes a card from circulation, which is the same power a
BLOCKED verdict has, so it arms deliberately and in stages.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Claim attempts a card may accumulate before it must explain itself. Matches
#: _MAX_CLAIMS in skfleet-rotate so the two gates agree on what "too many" is.
DEFAULT_MAX_CLAIM_ATTEMPTS = 5

#: Distinct owners that have attempted this card. Three different seats failing
#: the same card is a property of the CARD, not of any one seat: 83e498b6 drew
#: six owners and completed for none of them.
DEFAULT_MAX_OWNERS = 3

#: Floors. A threshold of 1 refuses a card on its FIRST honest retry, which is
#: ordinary and healthy, and would convert the breaker into a ban on retrying.
MIN_MAX_CLAIM_ATTEMPTS = 2
MIN_MAX_OWNERS = 2

MODES = ("off", "report", "enforce")

#: Actions that end a card's life. A card that completed or was voided is not
#: churning no matter how many claims it took to get there.
_TERMINAL_ACTIONS = frozenset({"complete", "void"})

#: Actions that end the CURRENT hold, so that a later claim by the same owner
#: is a fresh attempt rather than a duplicate of the one already held.
_RELEASE_ACTIONS = frozenset({"release_claim", "unassign", "demote", "reopen"})

#: Link keys that can carry a recorded blocker directly.
_BLOCKED_ON_KEYS = ("blocked_on",)


class ClaimRefusedError(ValueError):
    """A claim refused by the churn breaker.

    Subclasses ValueError on purpose: every claim path in this repo already
    handles ValueError from Board.claim_task, so a refusal reports through the
    machinery that is already there rather than through a new failure mode.
    """


@dataclass(frozen=True)
class ChurnSignature:
    """What the stores say about one card's claim history."""

    card_id: str
    claim_attempts: int
    distinct_owners: int
    terminal: bool
    blocker: str | None


@dataclass(frozen=True)
class ChurnVerdict:
    card_id: str
    refuse: bool
    reason: str
    signature: ChurnSignature


def mode_from_env(env: Mapping[str, str] | None = None) -> str:
    """off, report or enforce. Anything unrecognised is off."""
    source = os.environ if env is None else env
    raw = str(source.get("SKFLEET_CHURN_BREAKER_MODE", "")).strip().lower()
    return raw if raw in MODES else "off"


def _int_from_env(env: Mapping[str, str], name: str, default: int, floor: int) -> int:
    """A configured threshold, or the default for anything unusable.

    Parsed as a float before it is narrowed, because that is the only way to
    reject ``nan`` and ``inf`` deliberately. ``nan`` survives a ``< floor``
    test, since every comparison against nan is False, and it would then make
    ``attempts >= threshold`` False for EVERY card, silently disarming the
    breaker while the mode still read enforce. The same trap is documented on
    ``claim_expiry.ttl_seconds_from_env``.

    Never raises. This is read on the claim path, and a hard failure there
    costs more than an unexpectedly conservative threshold.
    """
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value < floor:
        return default
    return int(value)


def thresholds_from_env(env: Mapping[str, str] | None = None) -> tuple[int, int]:
    """(max claim attempts, max distinct owners)."""
    source = os.environ if env is None else env
    return (
        _int_from_env(
            source,
            "SKFLEET_CHURN_CLAIM_ATTEMPTS",
            DEFAULT_MAX_CLAIM_ATTEMPTS,
            MIN_MAX_CLAIM_ATTEMPTS,
        ),
        _int_from_env(source, "SKFLEET_CHURN_OWNERS", DEFAULT_MAX_OWNERS, MIN_MAX_OWNERS),
    )


def evaluate(
    signature: ChurnSignature, *, max_claim_attempts: int, max_owners: int
) -> ChurnVerdict:
    """Pure verdict for one signature. Never raises, never reads the world."""
    reason = ""
    refuse = False
    if signature.terminal:
        reason = "terminal"
    elif signature.blocker:
        # The blocker is what re-permits the claim. A card that says what is in
        # the way has already paid the price this gate exists to collect, and
        # the rest of the board (blocked_backoff, blocker_referent) decides
        # whether that reason is still live.
        reason = "blocker-recorded"
    elif signature.distinct_owners >= max_owners:
        reason = "churn-owners"
        refuse = True
    elif signature.claim_attempts >= max_claim_attempts:
        reason = "churn-claims"
        refuse = True
    else:
        reason = "under-threshold"
    return ChurnVerdict(
        card_id=signature.card_id, refuse=refuse, reason=reason, signature=signature
    )


def _card_events(home: Path, card_id: str) -> list[dict]:
    """Every event on a card, ordered by timestamp.

    Ordered by ``ts`` and NOT by ``seq``. Events shard one file per writer and
    ``seq`` restarts at 0 in each file, so a seq sort interleaves two writers'
    histories wrongly, which is the defect that lost 98 releases when
    claim_expiry replayed the log itself.

    Non-dict lines are skipped. A worker once appended four bare JSON STRINGS
    into card 7b7c990f's event log; ``json.loads`` accepts those, and one such
    line stopped the rotation on all five hosts for 40 minutes.
    """
    events_dir = Path(home) / "cards" / str(card_id) / "events"
    rows: list[dict] = []
    if not events_dir.is_dir():
        return rows
    try:
        names = sorted(events_dir.iterdir())
    except OSError:
        return rows
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
                if isinstance(event, dict):
                    rows.append(event)
    rows.sort(key=lambda e: (str(e.get("ts") or ""), str(e.get("event_id") or "")))
    return rows


def count_claim_attempts(events: list[dict]) -> tuple[int, int, bool]:
    """(attempts, distinct owners, terminal) folded from a card's event log.

    A worker re-claiming a card IT ALREADY HOLDS is normal and writes a second
    ``claim`` event; the fold settles both with one release. Counting claim
    events naively therefore over-counts exactly the healthy case, and on a
    long-running card it manufactures a churn signature out of one worker
    doing one job. So a claim by the owner that already holds the card is not
    an attempt. A release clears the hold, and a claim after it is a fresh
    attempt even from the same owner, because that IS a second dispatch.

    The hold tracked here is the last owner to ATTEMPT a claim, not the owner
    the fold settled on. The two differ when the fold refuses a concurrent
    claim (it records a claim_conflict and keeps the first owner), and the
    attempt is still what this gate measures: a refused claim burned a
    dispatch just the same.
    """
    attempts = 0
    owners: set[str] = set()
    held: str | None = None
    terminal = False
    for event in events:
        action = str(event.get("action") or "")
        if action in _TERMINAL_ACTIONS:
            terminal = True
            held = None
            continue
        if action in _RELEASE_ACTIONS:
            held = None
            continue
        if action != "claim":
            continue
        owner = str(event.get("owner") or event.get("writer") or "").strip()
        if not owner:
            continue
        if owner == held:
            continue
        attempts += 1
        owners.add(owner)
        held = owner
    return attempts, len(owners), terminal


def _blocker_from_mapping(source: Mapping[str, object] | None) -> str | None:
    """A recorded blocker in a folded card's links or meta, or None.

    Two shapes, both already in use and neither invented here:

      * a typed ``blocked_on`` link, parsed by ``review_admission``
      * an outcome link whose value is a BLOCKED verdict, validated by
        ``blocked_verdict``

    A bare category is not enough. "blocked_on: human" says a person is needed
    without saying which decision, and ``validate_blocked_verdict`` already
    refuses it for exactly that reason. Accepting it here would let a card
    re-permit itself with a word.
    """
    if not isinstance(source, Mapping):
        return None
    from ..blocked_verdict import blocked_on_referent, is_blocked_verdict
    from ..review_admission import parse_blocked_on_link

    for key in _BLOCKED_ON_KEYS:
        parsed = parse_blocked_on_link(source.get(key))
        if parsed is not None and parsed[1]:
            return f"blocked_on={parsed[0]} referent={parsed[1]}"
    for key, value in source.items():
        text = str(value or "")
        if is_blocked_verdict(str(key), text) and blocked_on_referent(text):
            return text.strip()[:200]
    return None


def _blocker_from_evidence(home: Path, card_id: str) -> str | None:
    """A BLOCKED verdict for this card in the coordination evidence store.

    ``coord link``, ``verdict`` and ``evidence`` land in
    ``coordination/card_events/*.jsonl`` and never touch the per-card shards,
    which is the two-store split that has caught this codebase repeatedly.
    Reading only the fold would make the breaker blind to the very verdict it
    tells workers to write.

    Only the LATEST outcome counts. A card blocked in June and passed in
    August is not blocked, and matching on the word BLOCKED anywhere in the
    history would hold it forever.
    """
    from ..blocked_verdict import blocked_on_referent, is_blocked_verdict, is_outcome_key

    events_dir = Path(home) / "coordination" / "card_events"
    if not events_dir.is_dir():
        return None
    try:
        names = sorted(events_dir.iterdir())
    except OSError:
        return None
    latest_ts = ""
    latest_value = ""
    needle = str(card_id)
    for name in names:
        if name.suffix != ".jsonl":
            continue
        try:
            handle = name.open(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with handle:
            for line in handle:
                # Cheap prefilter. Must admit PASS verdicts too, or a PASS that
                # supersedes a block would be invisible and the block eternal.
                if needle not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if str(event.get("card_id") or "") != needle:
                    continue
                key = str(event.get("link_key") or event.get("key") or "")
                if not key or not is_outcome_key(key):
                    continue
                value = str(event.get("link_value") or event.get("value") or "")
                stamp = str(event.get("ts") or "")
                if stamp >= latest_ts:
                    latest_ts = stamp
                    latest_value = value
    if latest_value and is_blocked_verdict("verdict", latest_value):
        if blocked_on_referent(latest_value):
            return latest_value.strip()[:200]
    return None


def read_signature(home: Path | str, card_id: str) -> ChurnSignature:
    """Fold one card's churn signature from the real stores.

    Terminal state comes from ``CardStore.fold`` rather than from a local
    replay, for the reason claim_expiry documents at length: a hand-rolled
    replay of this log diverged from the fold four separate ways. The event
    log is read only for what the fold does not retain, which is the claim
    history itself.
    """
    root = Path(home).expanduser()
    events = _card_events(root, card_id)
    attempts, owners, terminal_from_log = count_claim_attempts(events)

    terminal = terminal_from_log
    links: Mapping[str, object] | None = None
    meta: Mapping[str, object] | None = None
    try:
        from skcoord.card_store import CardStore

        card = CardStore(root).fold(str(card_id))
    except Exception:  # pragma: no cover - a fold failure must not block a claim
        card = None
    if card is not None:
        if getattr(card, "archived", False) or getattr(card.status, "value", "") == "done":
            terminal = True
        links = card.links if isinstance(card.links, Mapping) else None
        meta = card.meta if isinstance(card.meta, Mapping) else None

    blocker = _blocker_from_mapping(links) or _blocker_from_mapping(meta)
    if blocker is None:
        blocker = _blocker_from_evidence(root, card_id)
    return ChurnSignature(
        card_id=str(card_id),
        claim_attempts=attempts,
        distinct_owners=owners,
        terminal=terminal,
        blocker=blocker,
    )


def check_claim(
    home: Path | str, card_id: str, owner: str, env: Mapping[str, str] | None = None
) -> ChurnVerdict | None:
    """The verdict for one prospective claim, or None when the breaker is off.

    Returns before touching the filesystem in off mode, so an unarmed breaker
    costs one dict lookup on the claim path.
    """
    mode = mode_from_env(env)
    if mode == "off":
        return None
    max_claims, max_owners = thresholds_from_env(env)
    try:
        signature = read_signature(home, card_id)
    except Exception as exc:  # pragma: no cover - a reader fault must not deny work
        logger.warning("churn breaker could not read %s: %s", card_id, exc)
        return None
    verdict = evaluate(signature, max_claim_attempts=max_claims, max_owners=max_owners)
    if verdict.refuse:
        logger.warning(
            "churn breaker %s: card=%s owner=%s reason=%s claims=%d owners=%d",
            mode,
            verdict.card_id,
            owner,
            verdict.reason,
            signature.claim_attempts,
            signature.distinct_owners,
        )
    return verdict


def refusal_message(verdict: ChurnVerdict, owner: str) -> str:
    """What a refused claimant is told, including how to re-permit the card."""
    signature = verdict.signature
    return (
        f"claim refused by the churn breaker: card {verdict.card_id} has "
        f"{signature.claim_attempts} claim attempts across "
        f"{signature.distinct_owners} owners and has never completed "
        f"(reason={verdict.reason}, claimant={owner}). Record WHY it is stuck "
        "before claiming it again, with the blocker vocabulary the board "
        "already uses, for example: BLOCKED. blocked_on=human "
        "referent=approval:tailscale-admin-console, or blocked_on=dependency "
        "referent=card:04b218cd. Recording that blocker re-permits the claim. "
        "Another attempt without one burns a worker slot and returns the card "
        "exactly where it is."
    )


def assert_claim_permitted(
    home: Path | str, card_id: str, owner: str, env: Mapping[str, str] | None = None
) -> ChurnVerdict | None:
    """Raise ClaimRefusedError in enforce mode. Report mode logs and permits.

    This is the single skcapstone-side chokepoint. Every place in this repo
    that reaches ``Board.claim_task`` calls it first, so the CLI, the MCP tool,
    the auction, the shell, the spawner and the builder dispatcher all obey one
    rule. Board.claim_task itself lives in skcoord, a different repo, and
    callers outside this one are NOT covered by this function.
    """
    verdict = check_claim(home, card_id, owner, env)
    if verdict is not None and verdict.refuse and mode_from_env(env) == "enforce":
        raise ClaimRefusedError(refusal_message(verdict, owner))
    return verdict
