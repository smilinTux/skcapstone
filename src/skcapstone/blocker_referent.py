"""Return a card whose recorded blocker has since completed.

A worker that cannot finish a card records why in its verdict:

    BLOCKED. blocked_on=card referent=card:c818148b

That is a real blocking relationship, but it lives as prose inside an evidence
link rather than as a dependency edge on the card. Nothing on the board watches
it. When c818148b later completes, the card it was blocking stays blocked, and
the only thing that can return it is a person noticing.

MEASURED ON THE LIVE BOARD, 2026-08-28. Of the 14 cards most often cited as
blockers, SEVEN were already DONE. Sweeping every open card whose latest
outcome was BLOCKED:

    12  every cited blocker had completed
    41  still genuinely blocked
     0  citing a referent that does not exist

Those 12 were not waiting on work. They were waiting on someone to look.

WHY A SWEEP AND NOT A ROTATION CHANGE. The rotation decides what to launch and
runs on every host every cycle; this is a board repair that only needs to run
occasionally, and it mutates card labels rather than dispatching work. Keeping
it separate means the rotation stays a scheduler, and this stays auditable on
its own.

SAFE BY DEFAULT. Reports without mutating unless given --go, because returning
a card puts it back in front of a worker and that should be a deliberate act.

ON CONCURRENCY, AND WHY THE RACE IS LEFT IN. The once-per-verdict guard reads a
card's label and then writes it, and those two steps are not atomic across
hosts: SKCoord provides host-local card locks and writer-local transition IDs,
not global compare-and-append. Five hosts run this on a timer, so two can both
observe a card as unlabelled and both label it.

That race is real and is deliberately tolerated, because its consequence is
bounded to a redundant event. Labels are a set, so a doubly-labelled card is
labelled once. There is one card, so it returns to the pool once. No verdict is
rewritten, no approval is discharged, and nothing is cleared: this flags for
reconciliation and a reader still judges the evidence. The cost of losing the
race is a duplicate add_label line in a log.

Eliminating it would mean building fleet-wide compare-and-append, a primitive
nothing else here needs, to prevent a duplicate log entry. If the failure mode
were a lost verdict or a twice-discharged approval this would be the wrong call
and the guard would need a real lock.
"""

from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path

#: Link keys that carry a card's outcome. Matches the spellings the store
#: actually uses, the same shape-based test the blocked verdict validator uses.
_OUTCOME_KEY_RE = re.compile(
    r"(verdict|outcome|result|disposition|review_decision)", re.IGNORECASE
)

_BLOCKED_RE = re.compile(r"^\s*BLOCKED", re.IGNORECASE)

#: Cheap line-level prefilter so a full JSON parse is only paid for lines that
#: could carry an outcome at all.
_OUTCOME_HINT_RE = re.compile(r"verdict|outcome|result|disposition|review_decision", re.IGNORECASE)

#: A cited card, in the shapes workers write: `referent=card:c818148b`,
#: `referent: c818148b`, `Referent 04b218cd`. The prefix is optional because
#: both spellings appear on the board.
_REFERENT_RE = re.compile(r"referent[\"']?\s*[=:]?\s*[\"']?(?:card:)?([0-9a-f]{8})", re.IGNORECASE)

#: Link keys that name the card carrying out a repair or an independent
#: re-review of THIS card. When one of those passes, the block that named it is
#: answered, but nothing on the board propagates that back to the parent.
#:
#: MEASURED 2026-08-28. Twelve cards read BLOCKED whose own named successor had
#: already PASSED, most within fifteen minutes of the block:
#:
#:     72d9bfe5  blocked 07:12  independent_rereview 01cf9986 PASS 07:17
#:     8e33f3c3  blocked 07:53  rereview_card        473f24af PASS 08:04
#:     2ead9e49  blocked 09:48  successor_engineering f4d669ea PASS 09:58
#:
#: The fixes landed almost immediately. The verdicts were never updated, so the
#: board carried days of walls that no longer existed.
_SUCCESSOR_KEY_RE = re.compile(
    r"(repair_card|repair|rereview|re_review|reviewed_by|successor)", re.IGNORECASE
)

#: Evidence lives in the coordination store under link_key/link_value. The
#: bare key/value spelling is a legacy layout still present in older files.
_KEY_FIELDS = ("link_key", "key")
_VALUE_FIELDS = ("link_value", "value")


def _first(event: dict, fields: tuple[str, ...]) -> str:
    for f in fields:
        v = event.get(f)
        if v:
            return str(v)
    return ""


def is_blocked_outcome(key: str, value: str) -> bool:
    """True when this link records an outcome that declares the card BLOCKED."""
    return bool(_OUTCOME_KEY_RE.search(str(key or ""))) and bool(
        _BLOCKED_RE.match(str(value or ""))
    )


def cited_referents(value: str) -> list[str]:
    """Card ids named as the blocker by a BLOCKED verdict, lowercased."""
    return [m.group(1).lower() for m in _REFERENT_RE.finditer(str(value or ""))]


def _latest_blocked(home: Path) -> dict[str, tuple[str, str]]:
    """Map card id to its most recent BLOCKED verdict text.

    A card that was blocked and later passed must not appear here, so this
    tracks the latest outcome of ANY kind and keeps it only if that latest one
    is a refusal.
    """
    latest: dict[str, tuple[str, str]] = {}
    events = home / "coordination" / "card_events"
    for path in sorted(glob.glob(str(events / "*.jsonl"))):
        with open(path, errors="ignore") as fh:
            for line in fh:
                # Cheap prefilter. It must admit PASS verdicts too: a card
                # blocked in June and passed in August is not blocked, and
                # filtering on the word BLOCKED would hide the PASS that
                # supersedes it.
                if not _OUTCOME_HINT_RE.search(line):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                key = _first(event, _KEY_FIELDS)
                if not _OUTCOME_KEY_RE.search(key):
                    continue
                card_id = event.get("card_id")
                if not card_id:
                    continue
                stamp = str(event.get("ts") or "")
                value = _first(event, _VALUE_FIELDS)
                prev = latest.get(str(card_id))
                if prev is None or stamp >= prev[0]:
                    latest[str(card_id)] = (stamp, value)
    return {
        cid: (stamp, value)
        for cid, (stamp, value) in latest.items()
        if _BLOCKED_RE.match(value or "")
    }


def latest_blocked_verdicts(home: Path) -> dict[str, str]:
    """Map card id to its most recent BLOCKED verdict text."""
    return {cid: value for cid, (_stamp, value) in _latest_blocked(home).items()}


def last_labelled(home: Path, label: str) -> dict[str, str]:
    """Map card id to the last time `label` was applied to it.

    Labels are written to the coordination evidence store as add_label events,
    not to the card's own event log, which is the same two-store split that has
    caught this codebase before.
    """
    seen: dict[str, str] = {}
    events = home / "coordination" / "card_events"
    for path in sorted(glob.glob(str(events / "*.jsonl"))):
        with open(path, errors="ignore") as fh:
            for line in fh:
                if label not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("action") != "add_label" or event.get("label") != label:
                    continue
                card_id = str(event.get("card_id") or "")
                stamp = str(event.get("ts") or "")
                if card_id and stamp > seen.get(card_id, ""):
                    seen[card_id] = stamp
    return seen


def find_returnable(
    home: Path, is_done, is_open, label: str = "blocker-now-done"
) -> tuple[list[str], int, int]:
    """Cards whose every cited blocker has completed.

    Args:
        home: The .skcapstone root.
        is_done: Callable taking an 8-hex prefix, returning True if that card is
            DONE, False if not, and None if no such card exists.
        is_open: Callable taking a card id, returning True if it is still open.
        label: The return label. A card already carrying it from AFTER its
            current verdict has been returned for that verdict and is skipped,
            so the sweep is safe to run on a timer. A NEW refusal recorded
            after the label makes the card eligible again.

    Returns:
        (returnable card ids, count still blocked, count citing a missing card)
    """
    returnable: list[str] = []
    still_blocked = 0
    missing = 0
    labelled_at = last_labelled(home, label)
    for card_id, (stamp, verdict) in _latest_blocked(home).items():
        if not is_open(card_id):
            continue
        # Already returned for THIS refusal. Labelling does not erase the
        # verdict, so without this the same cards return on every run and
        # their backoff resets forever.
        if labelled_at.get(card_id, "") >= stamp and labelled_at.get(card_id):
            continue
        referents = cited_referents(verdict)
        if not referents:
            continue
        states = [is_done(r) for r in referents]
        if any(s is None for s in states):
            missing += 1
        elif all(states):
            returnable.append(card_id)
        else:
            still_blocked += 1
    return sorted(returnable), still_blocked, missing


def _verdict_head(value: str) -> str:
    """The leading token of a verdict, uppercased.

    A PASS routinely explains what it supersedes, so it contains the word
    BLOCKED in prose. Substring matching reads those as refusals; only the
    leading token is the verdict.
    """
    head = str(value or "").strip()
    for sep in ("|", ";", ":", ",", "."):
        head = head.split(sep)[0]
    return head.strip().split()[0].upper() if head.strip() else ""


#: A verdict that only declares work READY for review. It is not a pass and
#: must never discharge a block: 6dd21df9 records PASS_FOR_INDEPENDENT_REVIEW
#: and its own independent review, 335c91c6, then blocked. Treating the first
#: as a pass would have reported ae993252's block as stale while the review it
#: was waiting on had actually failed.
_PROVISIONAL_PASS_RE = re.compile(r"^PASS[_-](FOR|READY)", re.IGNORECASE)


def is_discharging_pass(value: str) -> bool:
    """True only for a completed PASS, not one awaiting its own review."""
    head = _verdict_head(value)
    if not head.startswith("PASS"):
        return False
    return not _PROVISIONAL_PASS_RE.match(head)


def latest_outcomes(home: Path) -> dict[str, tuple[str, str]]:
    """Every card's most recent outcome, as {card_id: (timestamp, value)}."""
    latest: dict[str, tuple[str, str]] = {}
    events = home / "coordination" / "card_events"
    for path in sorted(glob.glob(str(events / "*.jsonl"))):
        if ".bak" in path:
            continue
        with open(path, errors="ignore") as fh:
            for line in fh:
                if not _OUTCOME_HINT_RE.search(line):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not _OUTCOME_KEY_RE.search(_first(event, _KEY_FIELDS)):
                    continue
                card_id = str(event.get("card_id") or "")[:8]
                value = _first(event, _VALUE_FIELDS)
                if not card_id or not value:
                    continue
                stamp = str(event.get("ts") or "")
                if card_id not in latest or stamp >= latest[card_id][0]:
                    latest[card_id] = (stamp, value)
    return latest


def successor_links(home: Path) -> dict[str, list[tuple[str, str]]]:
    """Cards each card names as its repair or re-review, as {card: [(key, target)]}."""
    out: dict[str, list[tuple[str, str]]] = {}
    events = home / "coordination" / "card_events"
    for path in sorted(glob.glob(str(events / "*.jsonl"))):
        if ".bak" in path:
            continue
        with open(path, errors="ignore") as fh:
            for line in fh:
                if "link" not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                key = _first(event, _KEY_FIELDS)
                if not _SUCCESSOR_KEY_RE.search(key):
                    continue
                card_id = str(event.get("card_id") or "")[:8]
                target = _first(event, _VALUE_FIELDS).strip()[:8]
                if card_id and len(target) == 8 and target != card_id:
                    out.setdefault(card_id, []).append((key, target))
    return out


def find_stale_blocks(home: Path, is_open=None, label: str = "successor-passed") -> list[dict]:
    """Cards reading BLOCKED whose own named successor has since PASSED.

    The successor's PASS must come AFTER the block. A successor that passed
    earlier answered some previous refusal, not this one, and treating it as
    current would clear a live block on stale evidence.

    Deliberately does NOT skip closed cards, which is the difference between
    this and find_returnable. A stale verdict does its damage through whoever
    READS it, not through the card's own column. 2c35d28b folded to DONE and
    still held four approval gates shut for five days, because the gates read
    its verdict and saw BLOCKED. Filtering to open cards would have hidden the
    single worst instance on the board. is_open is accepted only so a caller
    can narrow the scan, and is ignored by default.
    """
    outcomes = latest_outcomes(home)
    links = successor_links(home)
    labelled_at = last_labelled(home, label)
    stale: list[dict] = []
    for card_id, (stamp, verdict) in outcomes.items():
        if not _verdict_head(verdict).startswith("BLOCK"):
            continue
        if is_open is not None and not is_open(card_id):
            continue
        if labelled_at.get(card_id, "") >= stamp and labelled_at.get(card_id):
            continue
        for key, target in links.get(card_id, []):
            hit = outcomes.get(target)
            if not hit:
                continue
            t_stamp, t_verdict = hit
            if is_discharging_pass(t_verdict) and t_stamp > stamp:
                stale.append(
                    {
                        "card": card_id,
                        "blocked_at": stamp,
                        "link": key,
                        "successor": target,
                        "passed_at": t_stamp,
                    }
                )
                break
    return sorted(stale, key=lambda r: r["card"])


def card_dir_lookup(home: Path):
    """Resolve an 8-hex prefix to a card directory name, or None."""
    cards = home / "cards"

    def lookup(prefix: str):
        matches = [os.path.basename(p) for p in glob.glob(str(cards / (prefix + "*")))]
        return matches[0] if matches else None

    return lookup


# --- returning a card whose blocker has already finished ----------------------
#
# WHAT IS BROKEN. The dispatcher parks a card whose latest outcome is BLOCKED
# and wakes it when the blocker CHANGES after the verdict
# (``_blocker_change_epoch``). That fence is exactly right for a blocker still
# in motion, and unreachable for one that was ALREADY terminal when the verdict
# was written: nothing further will ever happen to it, so no change can ever
# arrive, so the card is parked for good.
#
# MEASURED ON chiap01, 2026-09-18. 206 open cards carry a BLOCKED outcome. Of
# those, eleven name a referent whose fold is DONE and whose completion
# timestamp is EARLIER than their own verdict. 885037c0 lost that race by half
# a second: its referent acfede01 completed at 1788824635.5 and the block was
# written at 1788824636.
#
# WHY A DURABLE EVENT AND NOT A LOOSER PREDICATE. Widening
# ``_blocker_change_epoch`` to accept an older completion leaves no record of
# why a card came back, and worse, the wake generation it returns also fences
# retries (``_wake_retry_available`` counts launches after that generation). A
# generation in the past counts every past launch as a retry; a generation of
# "now" resets the fence every cycle and loops forever. A ``reopen`` event has
# neither problem: the dispatcher already honours it as the escape hatch, it
# names its actor and its reason, and it is written once per blocker
# generation.
#
# WHAT STAYS WITH A PERSON. A ``human`` hold, a ``capability`` hold and a block
# with no parsable reason are not answered by a card reaching DONE. They are
# left alone on purpose: silently returning them would destroy the one signal
# saying a person has to decide something.

#: Blocker categories a machine may discharge by itself. ``human`` needs an
#: operator, ``capability`` needs a router or an author, and an unparsed reason
#: means nobody knows what the wall is. None of the three is answered by a
#: referent card completing.
AUTO_REOPEN_CATEGORIES = frozenset({"dependency", "card"})

#: The writer the fleet sweep attributes its reopens to, and the reason string
#: it records. Both are read back by operators, so they are constants, not
#: ad-hoc strings at the call site.
REOPEN_WRITER = "fleet-blocker-referent-sweep"
REOPEN_REASON = "blocker-referent-settled"

#: A referent that is an exact card id, and nothing else. `card:57c301a1:` with
#: its trailing colon (seen on chiap01's 57c201a1) resolves to no card at all,
#: so it must not match.
_EXACT_CARD_REFERENT_RE = re.compile(r"^card:([0-9a-f]{8})$", re.IGNORECASE)


def blocker_generation_id(card_id: str, generation) -> str:
    """A deterministic token for one card's settled-blocker generation.

    The token is derived from the BLOCKERS, not from the verdict that cited
    them. That is what makes the sweep idempotent in both directions:

    - running it twice appends one event, because ``append_event`` returns the
      durable event for a repeated ``transition_id``;
    - a worker that re-blocks on the SAME already-finished referents produces
      the same token, so the card is NOT returned a second time and a person
      gets to ask why a worker keeps walling on finished work.

    A referent that completes AGAIN later carries a new timestamp, which is a
    genuinely new fact and earns a new token.
    """
    import hashlib

    parts = ";".join("%s@%d" % (ref, int(stamp)) for ref, stamp in sorted(generation))
    digest = hashlib.sha256(("%s|%s" % (card_id, parts)).encode("utf-8")).hexdigest()
    return "blocker-settled:" + digest[:16]


def settled_blocker_reopen(card_id: str, category, referents, referent_facts) -> dict:
    """Decide whether one BLOCKED card may return to the pool unattended.

    Args:
        card_id: The parked card.
        category: Its blocked_on category, or None when nothing parsed.
        referents: The referents its verdict cited, in any order.
        referent_facts: Callable taking an 8-hex card id and returning
            ``{"state", "human_gated", "outcome_blocked", "completed_at"}``.
            ``state`` uses the scheduler's own vocabulary, where ``complete``
            is the fold's DONE, ``void`` covers voided AND
            archived-without-completion, and ``missing`` is no such card.

    Returns:
        ``{"reopen", "hold", "referents", "generation", "transition_id"}``.
        ``hold`` names the exact guard that refused, which is what an operator
        reads to see why a card did NOT come back.
    """
    cited = [str(ref or "").strip().lower() for ref in (referents or []) if str(ref or "").strip()]
    result = {
        "reopen": False,
        "hold": None,
        "referents": cited,
        "generation": [],
        "transition_id": None,
    }
    if category not in AUTO_REOPEN_CATEGORIES:
        result["hold"] = "category:%s" % (category or "unparsed")
        return result
    if not cited:
        result["hold"] = "no-referent"
        return result
    generation = []
    for ref in cited:
        match = _EXACT_CARD_REFERENT_RE.match(ref)
        if not match:
            result["hold"] = "referent-not-a-card:%s" % ref
            return result
        facts = referent_facts(match.group(1).lower()) or {}
        state = str(facts.get("state") or "missing")
        if state != "complete":
            result["hold"] = "referent-%s" % state
            return result
        if facts.get("human_gated"):
            result["hold"] = "referent-human-gated"
            return result
        # A card can fold to DONE while its own latest outcome still reads
        # BLOCKED. Column is not evidence: three of chiap01's candidates
        # (885037c0, 9df37866, bd795bb2) are exactly that shape.
        if facts.get("outcome_blocked"):
            result["hold"] = "referent-outcome-blocked"
            return result
        completed_at = float(facts.get("completed_at") or 0.0)
        if completed_at <= 0:
            # No completion event to name. The reopen would be unattributable.
            result["hold"] = "referent-completion-unstamped"
            return result
        generation.append((ref, completed_at))
    result["generation"] = sorted(generation)
    result["transition_id"] = blocker_generation_id(card_id, generation)
    result["reopen"] = True
    return result
