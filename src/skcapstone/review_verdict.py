"""Refuse to complete a review that never said anything.

A review card exists to produce a judgement. Completing one without recording a
verdict is the worst available outcome, because the board then shows the review
as DONE and the parent card as reviewed, while nobody ever wrote down what was
found. Silence is indistinguishable from approval at a glance, and it is the
reading everyone takes.

MEASURED ON THE LIVE BOARD, 2026-08-28. Of 317 completed review cards:

    278  recorded a verdict
     39  recorded nothing at all

Twelve percent of every review this estate has ever run passed by silence. One
of them, a93fd881, has exactly three structural events, claim, claim, complete,
and zero evidence rows. It reviewed a published candidate and said nothing, and
the board counted it.

The cost is not theoretical. c0b5fdbf's review card is complete with no outcome,
so the only honest thing that can be said about that candidate is that nobody
knows whether it passed. The work of reviewing it has to be paid again.

WHY THE WRITE PATH. The verdict contract already refuses a BLOCKED verdict that
does not explain itself, and that rule works because it fires where the value is
written. Asking reviewers to remember does not work; the worker brief has always
told them to return an exact PASS or BLOCKED, and 39 did not.

DELIBERATELY NARROW. Only cards that identify themselves as reviews are checked,
and any recorded outcome satisfies it, including BLOCKED. This does not judge
the verdict, it requires that one exists.
"""

from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path

#: A card is a review when it says so in its title. The estate marks these with a
#: [REVIEW] tag, sometimes alongside a size tag, e.g. "[SKW-X-01][S][REVIEW]".
_REVIEW_TITLE_RE = re.compile(r"\[REVIEW", re.IGNORECASE)

#: Link keys that carry a verdict. Matched on shape rather than an exact list,
#: because the store has many spellings of the same idea.
_OUTCOME_KEY_RE = re.compile(
    r"(verdict|outcome|result|disposition|review_decision)", re.IGNORECASE
)


def is_review_card(title: str) -> bool:
    """True when this card identifies itself as a review."""
    return bool(_REVIEW_TITLE_RE.search(str(title or "")))


def recorded_verdict(card_id: str, home: Path) -> str | None:
    """Return this card's recorded verdict from the evidence store, or None.

    Reads the EVIDENCE store, which is where verdicts actually live. Reading only
    the structure store is the recurring mistake in this codebase and is exactly
    why other detectors reported zero while the board was full of counterexamples.
    """
    evidence_dir = Path(home) / "coordination" / "card_events"
    if not evidence_dir.is_dir():
        return None
    latest: tuple[str, str] | None = None
    for path in sorted(glob.glob(str(evidence_dir / "*.jsonl"))):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or card_id not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("card_id") != card_id:
                        continue
                    # The raw evidence store writes link_key/link_value. Some
                    # readers normalize those to key/value, so accept both. Getting
                    # this wrong is silent: the lookup simply finds nothing and the
                    # card looks verdict-less. Caught on the live board when this
                    # returned none for e9d7900e, which had recorded PASS.
                    key = str(row.get("link_key") or row.get("key") or row.get("raw_key") or "")
                    value = str(row.get("link_value") or row.get("value") or "")
                    if not value.strip() or not _OUTCOME_KEY_RE.search(key):
                        continue
                    stamp = str(row.get("ts") or "")
                    if latest is None or stamp >= latest[0]:
                        latest = (stamp, value)
        except OSError:
            continue
    return latest[1] if latest else None


def validate_review_completion(card_id: str, title: str, home: Path) -> None:
    """Raise ValueError if a review card is being completed with no verdict.

    Args:
        card_id: The card being completed.
        title: That card's title, used to decide whether it is a review.
        home: The agent home containing the evidence store.

    Raises:
        ValueError: If the card is a review and has recorded no verdict.
    """
    if not is_review_card(title):
        return
    if recorded_verdict(card_id, home):
        return
    raise ValueError(
        f"review card {card_id} has recorded no verdict, so it cannot be "
        "completed. A review exists to produce a judgement, and completing one "
        "silently marks the parent as reviewed while leaving no record of what "
        "was found. Record the outcome first, for example: "
        f"skcapstone coord link {card_id} verdict 'PASS ...' or a BLOCKED verdict "
        "naming blocked_on with a category and a referent. BLOCKED is a perfectly "
        "good answer here; saying nothing is not."
    )
