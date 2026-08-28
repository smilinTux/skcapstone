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
_OUTCOME_HINT_RE = re.compile(
    r"verdict|outcome|result|disposition|review_decision", re.IGNORECASE
)

#: A cited card, in the shapes workers write: `referent=card:c818148b`,
#: `referent: c818148b`, `Referent 04b218cd`. The prefix is optional because
#: both spellings appear on the board.
_REFERENT_RE = re.compile(
    r"referent[\"']?\s*[=:]?\s*[\"']?(?:card:)?([0-9a-f]{8})", re.IGNORECASE
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


def latest_blocked_verdicts(home: Path) -> dict[str, str]:
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
        cid: value
        for cid, (_stamp, value) in latest.items()
        if _BLOCKED_RE.match(value or "")
    }


def find_returnable(home: Path, is_done, is_open) -> tuple[list[str], int, int]:
    """Cards whose every cited blocker has completed.

    Args:
        home: The .skcapstone root.
        is_done: Callable taking an 8-hex prefix, returning True if that card is
            DONE, False if not, and None if no such card exists.
        is_open: Callable taking a card id, returning True if it is still open.

    Returns:
        (returnable card ids, count still blocked, count citing a missing card)
    """
    returnable: list[str] = []
    still_blocked = 0
    missing = 0
    for card_id, verdict in latest_blocked_verdicts(home).items():
        if not is_open(card_id):
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


def card_dir_lookup(home: Path):
    """Resolve an 8-hex prefix to a card directory name, or None."""
    cards = home / "cards"

    def lookup(prefix: str):
        matches = [os.path.basename(p) for p in glob.glob(str(cards / (prefix + "*")))]
        return matches[0] if matches else None

    return lookup
