#!/usr/bin/env python3
"""Keep the `ready` column stocked so the fleet has work to dispatch.

WHY THIS EXISTS.

The fleet can run dozens of workers concurrently and routinely runs two.
Measured on the chi estate 2026-09-19, with 15 seats free and every lane
target raised:

    SLOTS|chiap01|codex=0/8 glm=1/2 qwen=0/1 kimi=0/3|total_free=15
    POOL_V2|population=1533 ready=15 ineligible=1518
    CYCLE_RECEIPT|chiap01|launched=2|attempted=3

Lane capacity was never the constraint, and neither was the gateway: codex
holds 32 slots and has never exceeded a peak of 2. The constraint is that
the dispatcher draws candidates from `ready`, and the estate had **20 cards
in `ready` against 1134 live cards sitting in `backlog`**. Nothing moved a
card between those two columns, so the ready pool drained and stayed drained.
Raising a lane target cannot fix an empty pool, which is why several rounds
of lane tuning changed nothing.

WHAT IT PROMOTES.

Only cards that would actually dispatch if promoted. Promoting a card the
selector will silently withhold is worse than leaving it alone: it inflates
`ready`, hides the real shortfall, and the card churns the claim ceiling. So
every gate the selector applies downstream is applied here first, and a card
is skipped with a named reason rather than promoted hopefully.

The bound is deliberate. This tops the pool up toward a target depth and
promotes at most `--max-promote` per run, so a wrong rule costs a handful of
reversible `coord move` calls rather than a thousand.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

#: A card whose title carries no size marker, or more than one, fails
#: `_size_class_for` in the dispatcher, resolves to no logical route, and is
#: dropped from the candidate scan before any lane is consulted, emitting no
#: log line at all. Promoting one produces a card that silently never runs.
SIZE_MARKER = re.compile(r"\[(S|M|L|XL)\]")

#: Labels that mean "never dispatch this", mirroring the selector.
BLOCKING_LABELS = frozenset({"not-claimable", "sprint-container", "do-not-claim"})

#: The selector refuses a card whose title matches this unless it carries the
#: explicit `dispatch-approved` opt-in. Kept identical on purpose: a promoter
#: that admits what the selector refuses just moves the stall one step later.
SENSITIVE_TITLE = re.compile(
    r"capauth|credential|custody|issuer|secret|\bkey\b|rollback|deploy|production"
    r"|release|migrat",
    re.IGNORECASE,
)

PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "normal": 2, "low": 3}


def _terminal(card) -> bool:
    status = str(getattr(card, "status", "")).lower()
    meta = getattr(card, "meta", None) or {}
    return (
        "done" in status
        or bool(getattr(card, "archived", False))
        or bool(meta.get("voided"))
    )


def _seat_of(labels: list[str]) -> str | None:
    for label in labels:
        text = str(label).strip().lower()
        if text.startswith("seat-"):
            return text[len("seat-"):]
    return None


def _skip_reason(card, folded: dict, seats: set[str]) -> str | None:
    """Return why this card must not be promoted, or None when it may be."""
    title = str(getattr(card, "title", "") or "")
    labels = [str(x).lower() for x in (getattr(card, "labels", None) or [])]

    if len(SIZE_MARKER.findall(title)) != 1:
        return "size-marker-not-exactly-one"
    if BLOCKING_LABELS.intersection(labels):
        return "blocking-label"
    if getattr(card, "owner", None):
        return "already-owned"
    if SENSITIVE_TITLE.search(title) and "dispatch-approved" not in labels:
        return "sensitive-category"
    # A review card additionally needs a typed source binding and a reviewer
    # seat. Those are absent on the overwhelming majority of them, so a review
    # card promoted today is withheld the moment it is examined.
    if "review" in labels:
        return "review-card-needs-source-binding"
    seat = _seat_of(labels)
    if seat is not None and seat not in seats:
        return "seat-unprovisioned:%s" % seat
    for dep in getattr(card, "dependencies", None) or []:
        target = folded.get(str(dep))
        if target is None:
            return "dependency-unreadable"
        if not _terminal(target):
            return "dependency-open"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--home", default=os.path.expanduser("~/.skcapstone"))
    ap.add_argument("--target-ready", type=int, default=60,
                    help="desired depth of the ready column")
    ap.add_argument("--max-promote", type=int, default=25,
                    help="hard ceiling on promotions in a single run")
    ap.add_argument("--agent", default="backlog-promoter")
    ap.add_argument("--apply", action="store_true",
                    help="perform the moves; without it, print the plan only")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(args.home).parent))
    from skcoord.card_store import CardStore  # noqa: E402

    store = CardStore(args.home)
    cards_dir = Path(args.home) / "cards"

    folded: dict = {}
    for entry in os.listdir(cards_dir):
        try:
            card = store.fold(entry)
        except Exception:
            continue
        if card is not None:
            folded[entry] = card

    ready, backlog = [], []
    for cid, card in folded.items():
        status = str(getattr(card, "status", "")).lower()
        if _terminal(card):
            continue
        if "ready" in status:
            ready.append(cid)
        elif "backlog" in status:
            backlog.append((cid, card))

    placement_path = Path(args.home) / "coordination" / "seat-placement.json"
    try:
        seats = set(json.loads(placement_path.read_text()).get("seats", {}))
    except Exception as exc:
        print(f"seat placement unreadable ({exc}); refusing to promote", file=sys.stderr)
        return 2

    shortfall = max(0, args.target_ready - len(ready))
    budget = min(shortfall, args.max_promote)
    print(f"ready={len(ready)} target={args.target_ready} "
          f"backlog={len(backlog)} shortfall={shortfall} budget={budget}")
    if budget == 0:
        print("ready column is at target; nothing to do")
        return 0

    eligible, skipped = [], {}
    for cid, card in backlog:
        reason = _skip_reason(card, folded, seats)
        if reason is None:
            eligible.append((cid, card))
        else:
            skipped[reason] = skipped.get(reason, 0) + 1

    eligible.sort(key=lambda row: (
        PRIORITY_RANK.get(str(getattr(row[1], "priority", "")).lower(), 9),
        str(getattr(row[1], "created_at", "")),
        row[0],
    ))

    print("eligible=%d  skipped=%s" % (
        len(eligible),
        ",".join(f"{k}={v}" for k, v in sorted(skipped.items(), key=lambda x: -x[1])),
    ))

    selected = eligible[:budget]
    promoted = 0
    for cid, card in selected:
        title = str(getattr(card, "title", ""))[:70]
        if not args.apply:
            print(f"  WOULD PROMOTE {cid} | {title}")
            continue
        result = subprocess.run(
            ["skcapstone", "coord", "move", cid, "ready",
             "--home", args.home, "--agent", args.agent],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            promoted += 1
            print(f"  PROMOTED {cid} | {title}")
        else:
            print(f"  FAILED   {cid} | {result.stderr.strip()[:140]}")

    if args.apply:
        # Read the column back through a fresh fold rather than trusting the
        # exit codes above. A CLI that prints success and persists nothing is a
        # documented failure mode in this stack, so the count that matters is
        # the one observed after the writes, by a different path than wrote it.
        verify = CardStore(args.home)
        confirmed = sum(
            1 for cid, _ in selected
            if (lambda c: c is not None and "ready" in str(getattr(c, "status", "")).lower())(
                verify.fold(cid))
        )
        print(f"promoted={promoted} confirmed_ready_on_reread={confirmed}")
        return 0 if confirmed == promoted else 1

    print(f"dry run: {len(selected)} would be promoted (pass --apply to execute)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
