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

DELIBERATELY NARROW. Only cards that identify themselves as reviews or
rereviews are checked. PASS additionally requires the complete protected-branch
CI set. FAIL and structured BLOCKED remain terminal without waiting for CI,
because a reviewer must be able to stop an unsafe candidate immediately.
"""

from __future__ import annotations

import glob
import json
import re
from pathlib import Path

#: Governed review and rereview cards use either a terminal tag or a tag with an
#: embedded identifier, for example [REVIEW] or [REREVIEW-119db735].
_REVIEW_TITLE_RE = re.compile(r"\[RE(?:RE)?VIEW(?:\]|-)", re.IGNORECASE)

#: Link keys that carry a verdict. Matched on shape rather than an exact list,
#: because the store has many spellings of the same idea.
_OUTCOME_KEY_RE = re.compile(
    r"(verdict|outcome|result|disposition|review_decision)", re.IGNORECASE
)
_CHECK_KEY_RE = re.compile(r"(?:^|_)(?:check|checks|ci)(?:_|$)", re.IGNORECASE)
_SUCCESS_CHECK_STATES = frozenset({"success", "successful", "passed", "pass"})
_REQUIRED_CI_LINK_KEYS = frozenset(
    {
        "ci_check_docs",
        "ci_check_gitleaks",
        "ci_check_lint",
        "ci_check_shim_imports",
        "ci_check_python311",
        "ci_check_python312",
    }
)


def _is_terminal_verdict(value: str) -> bool:
    """Accept only canonical terminal verdicts, never lookalike prefixes."""
    verdict = str(value or "").strip()
    if verdict in {"PASS", "FAIL"}:
        return True
    if not verdict.startswith("BLOCKED "):
        return False
    fields = verdict.split()[1:]
    return any(
        field.startswith("blocked_on=") and field != "blocked_on=" for field in fields
    ) and any(field.startswith("referent=") and field != "referent=" for field in fields)


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


def unsuccessful_checks(card_id: str, home: Path) -> list[str]:
    """Return missing or non-successful required CI links."""
    evidence_dir = Path(home) / "coordination" / "card_events"
    latest: dict[str, tuple[str, str]] = {}
    for path in sorted(glob.glob(str(evidence_dir / "*.jsonl"))):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("card_id") != card_id or row.get("action") != "link":
                        continue
                    key = str(row.get("link_key") or row.get("key") or "")
                    value = str(row.get("link_value") or row.get("value") or "")
                    if not _CHECK_KEY_RE.search(key):
                        continue
                    candidate = (str(row.get("ts") or ""), value)
                    if key not in latest or candidate[0] >= latest[key][0]:
                        latest[key] = candidate
        except OSError:
            continue
    return sorted(
        key
        for key in _REQUIRED_CI_LINK_KEYS
        if key not in latest or str(latest[key][1]).strip().lower() not in _SUCCESS_CHECK_STATES
    )


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
    verdict = recorded_verdict(card_id, home)
    if verdict == "PASS":
        checks = unsuccessful_checks(card_id, home)
        if not checks:
            return
        raise ValueError(
            f"review card {card_id} has required checks that are not successful: "
            + ", ".join(checks)
        )
    if verdict and _is_terminal_verdict(verdict):
        return
    if verdict:
        raise ValueError(
            f"review card {card_id} has nonterminal verdict {verdict!r}; "
            "record terminal PASS, FAIL, or structured BLOCKED before completion"
        )
    raise ValueError(
        f"review card {card_id} has recorded no verdict, so it cannot be "
        "completed. A review exists to produce a judgement, and completing one "
        "silently marks the parent as reviewed while leaving no record of what "
        "was found. Record the outcome first, for example: "
        f"skcapstone coord link {card_id} verdict 'PASS ...' or a BLOCKED verdict "
        "naming blocked_on with a category and a referent. BLOCKED is a perfectly "
        "good answer here; saying nothing is not."
    )
