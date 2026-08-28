"""Refuse a BLOCKED verdict that does not say what is blocking.

A BLOCKED verdict is the only thing standing between a card and another attempt.
It is also the only artefact anyone reads when deciding what to do about that
card. When it says nothing but the word BLOCKED, it stops the card AND tells
nobody why, which is the worst of both: the card is out of circulation and no
one can act to return it.

MEASURED ON THE LIVE BOARD, 2026-08-27. Of 39 open cards whose latest recorded
outcome was BLOCKED:

    18  the literal 7 characters "BLOCKED"
    20  prose with no blocked_on field at all
     1  a properly referenced blocked_on

Median verdict length was 29 characters. That pool had not drained all day,
because each card returned to the pool, got worked, recorded nothing usable, and
went straight back into backoff. The contract was already written into the worker
brief. Asking was not enough, so this refuses at the write path instead.

WHAT COUNTS AS SUFFICIENT. The verdict must name a category and a referent:

    dependency   something else on the board must land first
    card         a specific card is in the way
    human        a person must decide
    capability   the agent cannot do this at all

and then say WHICH. "blocked_on: human" is still unactionable; "blocked_on:
human, approval:sklegal_runtime-custody-path" is not.

Deliberately tolerant about SHAPE. Workers write JSON, prose and key=value, and
all three are fine. This checks that the information is present, not that it is
formatted a particular way, because a validator that rejects a truthful verdict
on punctuation teaches workers to fight the validator instead of to explain
themselves.
"""

from __future__ import annotations

import re

#: Link keys that carry a card's outcome. The store has many spellings of this
#: idea, so match on the shape of the key rather than an exact list.
_OUTCOME_KEY_RE = re.compile(
    r"(verdict|outcome|result|disposition|review_decision)", re.IGNORECASE
)

#: A verdict that begins this way is a refusal and must justify itself.
_BLOCKED_RE = re.compile(r"^\s*BLOCKED", re.IGNORECASE)

#: The categories a refusal may fall into.
_CATEGORIES = ("dependency", "card", "human", "capability")

_BLOCKED_ON_RE = re.compile(r"blocked[_\s-]?on", re.IGNORECASE)

#: Tokens that sit between a category and its referent in real verdicts:
#: `blocked_on=human referent approval:x`, and JSON's `"value": "human",
#: "referent": "approval:x"`. They are filler, not the answer.
_FILLER = {
    "referent",
    "value",
    "is",
    "to",
    "on",
    "the",
    "a",
    "an",
    "of",
    "for",
    "blocked",
    "blocked_on",
}

#: Words that are a category restated rather than an actual referent.
_NOT_A_REFERENT = (
    {c.lower() for c in _CATEGORIES}
    | _FILLER
    | {
        "none",
        "null",
        "unknown",
        "tbd",
        "n/a",
        "na",
        "pending",
        "true",
        "false",
    }
)

_CATEGORY_RE = re.compile(r"\b(%s)\b" % "|".join(_CATEGORIES), re.IGNORECASE)

#: A `card` refusal says a criterion cannot be satisfied AS WRITTEN. That claim is
#: only actionable if it also says WHY, because the fix is to rewrite the criterion
#: and nobody can rewrite what they cannot see is wrong.
#:
#: MEASURED ON THE LIVE BOARD, 2026-08-28. Of 16 open cards refused with
#: blocked_on=card, 13 recorded nothing but a pointer:
#:
#:     BLOCKED blocked_on=card referent=card:95e192fd criterion=ac:5
#:
#: That names which criterion and not one word about what is wrong with it. Those
#: 13 could not be amended without inventing a diagnosis, so they sat. The 3 that
#: did explain themselves were fixable immediately, and two were fixed the same
#: hour: one criterion was circular, requiring evidence of every acceptance
#: statement including itself, and one asked a worker to "prove whether" something
#: was possible, which gave a correct negative finding no way to pass.
#:
#: The worker brief already asks for this: "Choose card only when you can quote the
#: criterion and state the contradiction." Asking was not enough, so this refuses
#: at the write path, exactly as the referent rule does.
_CONTRADICTION_RE = re.compile(
    r"\b(requires?|required|prohibit\w*|forbid\w*|contradict\w*|conflict\w*|"
    r"absent|missing|does not exist|do not exist|never|cannot|can not|unsatisfiable|"
    r"impossible|because|while|whereas|but the|no such|not present|undefined|"
    r"circular|ambiguous)\b",
    re.IGNORECASE,
)

#: How much text past the pointer counts as an explanation attempt. A verdict that
#: is nothing but `blocked_on=card referent=ac:2` has no room for one.
_MIN_CONTRADICTION_CHARS = 24

#: The tell that a `card` refusal is about a CRITERION rather than about another
#: card. `blocked_on=card referent=inc-0e190b2f` names a different card and is
#: self-explanatory. `blocked_on=card referent=card:<self> criterion=ac:5` is not:
#: the referent points at the card writing it, and the real claim hides in a field
#: nothing validates. When criterion= is present, the contradiction is mandatory.
_CRITERION_RE = re.compile(r"criterion\s*[=:]\s*ac\s*:?\s*\d+", re.IGNORECASE)


def states_a_contradiction(value: str) -> bool:
    """True when a `card` refusal says what is wrong, not only which criterion.

    Deliberately generous about wording. Any of the ordinary ways a person
    explains an impossibility counts, because the goal is a usable sentence, not
    a particular phrasing. What it rejects is the bare pointer.
    """
    text = str(value or "")
    if len(text.strip()) < _MIN_CONTRADICTION_CHARS:
        return False
    return bool(_CONTRADICTION_RE.search(text))


#: A referent identifies a thing: a card id, an approval name, a path-like token.
_TOKEN_RE = re.compile(r"[A-Za-z0-9][\w:.\-/@]{2,}")

#: How far past a category to look. Long enough for JSON punctuation and a
#: filler word, short enough that unrelated prose later in the verdict is not
#: mistaken for a referent.
_REFERENT_WINDOW = 80

#: Categories whose referent must identify an actual card on the board.
#: `dependency` and `card` both assert "something else on the board is in the
#: way", and that claim is only checkable if the something else is named.
_CARD_CATEGORIES = {"dependency", "card"}

#: A card id: eight or more hex characters, optionally behind an ITIL kind
#: prefix (inc-, prb-, chg-). Accepts a `card:` qualifier in front.
_CARD_ID_RE = re.compile(r"(?:card[:\s]+)?(?:[a-z]{3}-)?[0-9a-f]{8,}\b", re.IGNORECASE)


def is_outcome_key(key: str) -> bool:
    """True when this link key records a card's outcome."""
    return bool(_OUTCOME_KEY_RE.search(str(key or "")))


def is_blocked_verdict(key: str, value: str) -> bool:
    """True when this link is an outcome that declares the card BLOCKED."""
    return is_outcome_key(key) and bool(_BLOCKED_RE.match(str(value or "")))


def blocked_on_referent(value: str) -> str | None:
    """Return the referent named by a BLOCKED verdict, or None.

    A category on its own is not a referent: "blocked_on: human" says a person
    is needed without saying which decision, which cannot be acted on. Filler
    between the category and the referent is expected and skipped, because
    workers write JSON, prose and key=value and all three are legitimate.
    """
    text = str(value or "")
    anchor = _BLOCKED_ON_RE.search(text)
    if not anchor:
        return None
    tail = text[anchor.end() :]
    for cat in _CATEGORY_RE.finditer(tail):
        window = tail[cat.end() : cat.end() + _REFERENT_WINDOW]
        for tok in _TOKEN_RE.finditer(window):
            candidate = tok.group(0).strip().strip(".,;:\"'")
            if not candidate:
                continue
            if candidate.lower() in _NOT_A_REFERENT:
                continue
            if candidate.lower().rstrip("s") in _NOT_A_REFERENT:
                continue
            return candidate
    return None


def validate_blocked_verdict(key: str, value: str) -> None:
    """Raise ValueError if a BLOCKED verdict does not say what is blocking.

    Args:
        key: The link key being written.
        value: The link value being written.

    Raises:
        ValueError: If the verdict declares BLOCKED without naming a category
            and a referent.
    """
    if not is_blocked_verdict(key, value):
        return
    text = str(value or "")
    if not _BLOCKED_ON_RE.search(text):
        raise ValueError(
            "a BLOCKED verdict must record blocked_on. Name a category "
            f"({', '.join(_CATEGORIES)}) and the exact thing it refers to, for "
            "example: blocked_on=dependency referent=card:04b218cd, or "
            "blocked_on=human referent=approval:sklegal_runtime-custody-path. "
            "A verdict that stops a card without saying why removes it from the "
            "pool and leaves nobody able to return it."
        )
    referent = blocked_on_referent(text)
    if referent is None:
        raise ValueError(
            "blocked_on names no referent. A category on its own is not "
            "actionable: say WHICH dependency, WHICH card, WHICH decision, or "
            "WHICH capability is missing. "
            f"Categories are: {', '.join(_CATEGORIES)}."
        )
    # A referent for `dependency` or `card` must identify a real card. Anything
    # else makes the claim uncheckable: nobody can act on "blocked by card ac:1",
    # because ac:1 is an acceptance criterion, not a card, and no query returns
    # it. Observed on the live board 2026-08-27 on card 16bbc6fe, which satisfied
    # the shape of this validator while remaining exactly as unactionable as the
    # bare BLOCKED verdicts it was written to replace.
    #
    # human and capability keep free-form referents on purpose: an approval name
    # or a missing capability has no id, and demanding one would push workers
    # back toward saying nothing.
    anchor = _BLOCKED_ON_RE.search(text)
    tail = text[anchor.end() :] if anchor else text
    cat = _CATEGORY_RE.search(tail)
    if cat and cat.group(1).lower() in _CARD_CATEGORIES:
        if not _CARD_ID_RE.search(referent):
            raise ValueError(
                f"blocked_on={cat.group(1).lower()} needs a card id, not "
                f"{referent!r}. Name the card that is in the way, for example "
                "blocked_on=dependency referent=card:04b218cd or "
                "referent=inc-0e190b2f. If the blocker is not a card, use "
                "blocked_on=human or blocked_on=capability instead, which take "
                "free-form referents."
            )
    # A `card` refusal must also say WHY, not only WHICH criterion.
    #
    # The two rules interact badly without this, and the board shows it. The brief
    # tells a worker to write `card referent ac:<n>`; the rule above demands a card
    # id instead. A worker satisfies both by naming ITS OWN card as the blocker and
    # putting the real information in a `criterion=` field that nothing checks:
    #
    #     BLOCKED blocked_on=card referent=card:95e192fd criterion=ac:5
    #                                            ^ on card 95e192fd itself
    #
    # That passes, and says nothing. Measured 2026-08-28: 13 of 16 card refusals had
    # exactly this shape, and none could be amended without inventing a diagnosis.
    # The 3 that explained themselves were actionable immediately, and two were
    # fixed within the hour: one criterion was circular, demanding evidence of every
    # acceptance statement including itself, and one asked a worker to "prove
    # whether" something held, which gave a correct negative finding no way to pass.
    #
    # So a card refusal must carry a sentence a person can act on. dependency does
    # not need this: naming a blocking card IS the explanation. Naming a criterion
    # is not, because the only fix is to rewrite that criterion, and nobody can
    # rewrite a fault they cannot see.
    if (
        cat
        and cat.group(1).lower() == "card"
        and _CRITERION_RE.search(text)
        and not states_a_contradiction(text)
    ):
        raise ValueError(
            "blocked_on=card names a criterion but not the contradiction. Say WHY "
            "it cannot be satisfied as written: state what the criterion requires "
            "that is impossible, absent, or self-referential. For example: "
            "blocked_on=card referent=card:16bbc6fe criterion=ac:2, AC2 requires "
            "install and restart while the standing rails prohibit deploy. A bare "
            "criterion=ac:N cannot be acted on, because the only fix is to rewrite "
            "that criterion."
        )
