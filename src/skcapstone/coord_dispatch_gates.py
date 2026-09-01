"""Why a card will, or will not, be dispatched by the fleet selector.

The authority is `scripts/fleet/skfleet-rotate.py`. These constants are copied
from it deliberately rather than imported: the selector is a standalone script
deployed to ~/.local/bin and is not importable from this package. If the two
ever disagree the selector wins and this module is stale.

This exists because on 2026-09-01 a card carrying a signed human authorization
with a fixed 30 minute issue window could not be dispatched, because its author
put [HUMAN] in the title. Titles and initial_labels are immutable, so once the
approval arrived there was no way to release the card. The gate was correct.
The card was authored wrong, and nothing told the author at creation time.

See docs/fleet/card-authoring-dispatch-gates.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NOT_CLAIMABLE = frozenset({"not-claimable", "sprint-container", "do-not-claim"})

NON_IMPLEMENTATION = frozenset({
    "planning-only-container",
    "do-not-claim-as-implementation",
    "human-gate",
    "human-decision-recorded-no-action",
    "no-action-authorized",
})

SENSITIVE_CATEGORY = re.compile(
    r"(capauth|credential|custody|issuer|secret|\bkey\b|rollback|"
    r"deploy|production|release|migrat)",
    re.I,
)
CATEGORY_OPT_IN = "dispatch-approved"

GOVERNED_CLASS = re.compile(r"\[(REVIEW|REREVIEW|REPAIR)\]", re.I)
PARENT_PREFIX = "parent-"


@dataclass(frozen=True)
class Gate:
    """One reason a card is or is not dispatchable.

    `removable` is the field that matters. A blocking gate you can clear with a
    label is a workflow step. A blocking gate baked into the title is permanent,
    and the card can only ever be run by hand.
    """

    name: str
    blocks: bool
    removable: bool
    detail: str


def normalise(labels) -> set[str]:
    return {str(x).strip().lower().replace("_", "-") for x in (labels or [])}


def evaluate(title: str, labels) -> list[Gate]:
    """Return every dispatch gate for a card, blocking or not."""
    title = str(title or "")
    norm = normalise(labels)
    gates: list[Gate] = []

    hit = sorted(norm & NOT_CLAIMABLE)
    gates.append(Gate(
        "not-claimable", bool(hit), True,
        f"labels {hit} keep this out of the pool; remove with "
        f"`coord label <id> {hit[0]} --remove`" if hit
        else "no not-claimable label",
    ))

    by_title = "[HUMAN]" in title.upper()
    by_label = "human-gate" in norm
    gates.append(Gate(
        "human-gate", by_title or by_label, not by_title,
        "[HUMAN] IN TITLE. Titles are immutable, so this gate can NEVER be "
        "removed and the card can only be run by hand. Use a `human-gate` "
        "LABEL instead." if by_title
        else "human-gate label set; remove it when the human decides" if by_label
        else "not human gated",
    ))

    nonimpl = sorted(norm & NON_IMPLEMENTATION)
    gates.append(Gate(
        "non-implementation", bool(nonimpl), True,
        f"labels {nonimpl} mark this as not a unit of work" if nonimpl
        else "treated as implementable work",
    ))

    blob = title + " " + " ".join(sorted(norm))
    match = SENSITIVE_CATEGORY.search(blob)
    opted_in = CATEGORY_OPT_IN in norm
    gates.append(Gate(
        "sensitive-category", bool(match) and not opted_in, True,
        f"matched {match.group(1).lower()!r} in the title or labels; add "
        f"`{CATEGORY_OPT_IN}` to opt in" if match and not opted_in
        else f"matched {match.group(1).lower()!r} and {CATEGORY_OPT_IN} is set"
        if match else "not a sensitive category",
    ))

    governed = GOVERNED_CLASS.search(title)
    parents = {x for x in norm if x.startswith(PARENT_PREFIX) and x != PARENT_PREFIX}
    gates.append(Gate(
        "governed-class", bool(governed) and len(parents) != 1, True,
        f"title declares [{governed.group(1).upper()}] so exactly one "
        f"parent-<cardid> label is required, found {len(parents)}"
        if governed and len(parents) != 1
        else f"[{governed.group(1).upper()}] with parent {sorted(parents)[0]}"
        if governed else "not a governed class",
    ))

    return gates


def blocking(gates) -> list[Gate]:
    return [g for g in gates if g.blocks]


def permanent(gates) -> list[Gate]:
    """Blocking gates that no later action can clear. These are the dangerous ones."""
    return [g for g in gates if g.blocks and not g.removable]
