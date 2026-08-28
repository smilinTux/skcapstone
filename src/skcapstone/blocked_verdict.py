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
#: Matched loosely on purpose: a criterion is named as `criterion=ac:2`, but also
#: bare as `ac=2` or `|ac:2|` inside a pipe-delimited verdict. Measured
#: 2026-08-28 on the live board: of 52 blocked_on=card verdicts, 31 spelled it
#: the long way and 14 used the short form. Matching only the long form let 27%
#: of card refusals record a criterion with no contradiction, which is exactly
#: the shape this rule exists to refuse. The remaining 7 name no criterion at
#: all and are already refused by the referent rule, since `ac:1` is not a card
#: id.
_CRITERION_RE = re.compile(r"(?:criterion\s*[=:]\s*)?\bac\s*[=:]\s*\d+", re.IGNORECASE)

#: A BLOCKED verdict ends this agent's turn on the card. Whoever picks it up next
#: inherits whatever was written down and nothing else, so the verdict is the
#: entire handover.
#:
#: MEASURED ON THE LIVE BOARD, 2026-08-28, across 198 BLOCKED cards:
#:
#:     machine-readable blocker    50%
#:     hashed artifact             28%
#:     what was attempted           7%
#:     where the live work sits     3%
#:     how to resume                0%
#:
#: The structural half is better than those numbers suggest: 66% left evidence at
#: the derivable path evidence/work/<card_id>/, none of it empty, so a successor
#: CAN find the artifacts. What no verdict carries is the expensive half, which is
#: what was tried and what to do next. Zero of 198 said how to resume, so every
#: successor re-pays the predecessor's discovery.
#:
#: The worker brief already asks for this: "Say what you attempted, so the next
#: attempt does not re-pay your discovery." 7% complied. Asking has not worked for
#: any convention on this board; enforcing at the write path has.
_RESUME_RE = re.compile(
    r"\b(attempt\w*|tried|reproduc\w*|verified|checked|ran|examined|inspected|"
    r"to resume|to unblock|next step|next attempt|required to resume|needs?\s+\w+|"
    r"re-?run|retry|once\b|after\b|when\b)\b",
    re.IGNORECASE,
)

#: Where the live work sits, if anywhere. An explicit "none" is a valid and useful
#: answer: it distinguishes "I produced nothing" from "I produced something and did
#: not tell you", and today those two look identical from the outside.
#:
#: That distinction has already cost this estate real work. Commits 229336b2 and
#: 22a36166 were recorded as permanently unverifiable because nobody knew whether
#: bytes existed; 22a36166's candidate was later found intact in a bundle in its own
#: evidence directory, 241 commits, simply unreferenced.
_WORK_LOCATION_RE = re.compile(
    r"\b(branch|commit\s+[0-9a-f]{7,}|bundle|worktree|pull/\d+|\bPR\b|"
    r"no_pr|no\s+pr|no\s+repository\s+change|no\s+branch|nothing\s+produced|"
    r"produced\s+no|no\s+artifact|no\s+candidate|work_location\s*[=:]\s*none)\b",
    re.IGNORECASE,
)


def states_how_to_resume(value: str) -> bool:
    """True when a refusal says what was tried or what to do next."""
    return bool(_RESUME_RE.search(str(value or "")))


def states_where_the_work_is(value: str) -> bool:
    """True when a refusal says where any live work sits, including 'none'."""
    return bool(_WORK_LOCATION_RE.search(str(value or "")))


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

    # HANDOVER. A BLOCKED verdict hands the card to whoever comes next, and the
    # verdict is the whole handover. These two checks apply to every category,
    # because a successor needs the same two facts regardless of what blocked it.
    # dependency is exempt from the resume requirement, and only dependency. Its
    # referent IS the resume condition: "blocked_on=dependency referent=card:04b218cd"
    # already tells a successor exactly when to try again, and the referent rule above
    # has already forced that card id to be real. Demanding a separate resume sentence
    # there would be asking a worker to restate what it just said.
    #
    # Every other category needs it. card names a criterion, which says nothing about
    # what was tried. human names an approval, which says nothing about what state the
    # work reached while waiting. capability says a stronger model is needed, which is
    # exactly when knowing what was already attempted is worth most.
    #
    # Measured effect of the exemption on the live board: 16 of 183 refusals.
    if cat and cat.group(1).lower() == "dependency":
        return
    if not states_how_to_resume(text):
        raise ValueError(
            "a BLOCKED verdict must leave a warm handover. Say what you ATTEMPTED "
            "and what the next agent should do first, so the next attempt does not "
            "re-pay your discovery. Measured 2026-08-28: 0 of 198 blocked cards "
            "said how to resume, so every successor started from nothing."
        )
    if not states_where_the_work_is(text):
        raise ValueError(
            "a BLOCKED verdict must say WHERE any live work sits: a branch, commit, "
            "bundle, worktree or PR. If you produced nothing, say so explicitly, for "
            "example 'no repository change, so no PR'. An explicit none is a good "
            "answer; silence is not, because it is indistinguishable from having "
            "produced something and not said where it is."
        )
