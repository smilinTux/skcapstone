#!/usr/bin/env python3
"""Keep the `ready` column stocked so the fleet has work to dispatch.

WHY THIS EXISTS.

The fleet can run dozens of workers concurrently and was running two. Measured
on the chi estate 2026-09-19, with 15 seats free and every lane target raised:

    SLOTS|chiap01|codex=0/8 glm=1/2 qwen=0/1 kimi=0/3|total_free=15
    POOL_V2|population=1533 ready=15 ineligible=1518
    CYCLE_RECEIPT|chiap01|launched=2|attempted=3

Lane capacity was never the constraint, and neither was the gateway: codex
holds 32 slots and has never peaked above 2. The dispatcher draws candidates
from `ready`, and the estate held 20 ready cards against 1134 live backlog
cards. Nothing moved a card between those columns, so the pool drained and
stayed drained. That is why several rounds of lane tuning changed nothing.

MIRRORING THE GATE, NOT GUESSING AT IT.

A first version of this script invented its own eligibility rules. It promoted
25 cards and the selector accepted 3: the rest carried `no-action`,
`human-gate` or an unsatisfied dependency and were refused downstream. That is
the worst possible outcome, because `ready` now reports depth the fleet cannot
use and the real shortfall is hidden.

So every rule below mirrors `_claimability_reason()` in `skfleet-rotate.py`,
named line by line, and the promoter refuses anything it cannot evaluate. When
that function changes, this one is wrong until it is updated to match: the
`REASON_SOURCE` map exists to make that coupling searchable rather than
discovering it again through another silent stall.

The bound is deliberate. This tops the pool up toward a target depth and moves
at most `--max-promote` per run, so a wrong rule costs a handful of reversible
`coord move` calls rather than a thousand.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

#: Mirrors `_NON_IMPLEMENTATION_LABELS` + `non_implementation()`.
#: Labels are normalized with `_` to `-` exactly as the dispatcher does.
NON_IMPLEMENTATION_LABELS = frozenset({
    "planning-only-container",
    "do-not-claim-as-implementation",
    "human-gate",
    "human-decision-recorded-no-action",
    "no-action-authorized",
})

#: Mirrors `_NOT_CLAIMABLE`. Checked against labels AND tags, not labels alone.
NOT_CLAIMABLE = frozenset({"not-claimable", "sprint-container", "do-not-claim"})

#: Mirrors `_SENSITIVE_CATEGORY` and `_CATEGORY_OPT_IN`.
SENSITIVE_CATEGORY = re.compile(
    r"(capauth|credential|custody|issuer|secret|\bkey\b|rollback|"
    r"deploy|production|release|migrat)", re.I)
CATEGORY_OPT_IN = "dispatch-approved"

#: Mirrors `_GATE_LANGUAGE_RE`, applied to acceptance criteria when
#: `spec_version >= 2`. Criteria that can only be met by someone else reviewing
#: are not satisfiable by the worker that would be dispatched.
GATE_LANGUAGE = re.compile(
    r"independent review|review pass|before merge|approved by|sign-?off"
    r"|reviewed by|merged to main|awaiting review", re.I)

#: Title markers that make a card review work regardless of its labels.
REVIEW_TITLE_MARKER = re.compile(r"\[(RE-?REVIEW|REVIEW)\]", re.I)

#: A card with no size marker, or more than one, fails `_size_class_for`,
#: resolves to no logical route, and is dropped from the candidate scan with
#: no log line at all. This is not in `_claimability_reason`; it bites later.
SIZE_MARKER = re.compile(r"\[(S|M|L|XL)\]")

PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "normal": 2, "low": 3}

#: Which upstream rule each refusal mirrors, so the coupling is greppable.
REASON_SOURCE = {
    "non-task": "_coord_task_claimable: kind must be task",
    "terminal": "_claimability_reason: void/archive/done",
    "owned": "_claimability_reason: owner set",
    "human-gate": "non_implementation: label set or [HUMAN] in title",
    "foreign-project": "_claimability_reason: foreign-project label",
    "not-claimable": "_NOT_CLAIMABLE against labels and tags",
    "sensitive-category": "_SENSITIVE_CATEGORY without dispatch-approved",
    "criteria-not-satisfiable": "_GATE_LANGUAGE_RE on spec_version>=2 criteria",
    "dependency-open": "_dep_satisfied: dependency not complete",
    "dependency-blocked": "_dep_satisfied: dependency completed BLOCKED",
    "review-lane": "_claimability_reason: review markers route elsewhere",
    "seat-unprovisioned": "_seat_owner: seat absent from seat-placement.json",
    "size-marker": "_size_class_for: exactly one [S]/[M]/[L]/[XL] required",
}


def _norm(values) -> set[str]:
    return {str(v).strip().lower().replace("_", "-") for v in (values or [])}


def _status(card) -> str:
    return str(getattr(card, "status", "")).lower().replace("column.", "")


def _terminal(card) -> bool:
    meta = getattr(card, "meta", None) or {}
    return (
        _status(card) == "done"
        or bool(getattr(card, "archived", False))
        or bool(meta.get("voided"))
    )


def _verdict_is_blocked(home: Path, cid: str) -> bool:
    """True when this card's recorded verdict says BLOCKED.

    Mirrors the second half of `_dep_satisfied`. A dependency that completed
    BLOCKED is not satisfied: the lifecycle says complete while the evidence
    says the foundation does not exist. Checking lifecycle alone is the
    joined-truth error this estate keeps repeating.
    """
    events_dir = home / "cards" / cid / "events"
    latest = None
    if events_dir.is_dir():
        for name in os.listdir(events_dir):
            try:
                with open(events_dir / name, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        try:
                            row = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(row, dict):
                            continue
                        if row.get("link_key") == "verdict":
                            key = (str(row.get("ts") or ""), int(row.get("seq") or 0))
                            if latest is None or key > latest[0]:
                                latest = (key, str(row.get("link_value") or ""))
            except OSError:
                continue
    return bool(latest and re.match(r"^\s*BLOCKED", latest[1], re.I))


def refusal(home: Path, card, folded: dict, seats: set[str]) -> str | None:
    """Return why this card must not be promoted, or None when it may be."""
    title = str(getattr(card, "title", "") or "")
    labels = _norm(getattr(card, "labels", None) or [])
    meta = getattr(card, "meta", None) or {}
    tags = _norm(meta.get("tags") or [])

    if str(getattr(card, "kind", "")).lower().replace("kind.", "") != "task":
        return "non-task"
    if _terminal(card):
        return "terminal"
    if getattr(card, "owner", None):
        return "owned"
    # non_implementation() checks labels AND a [HUMAN] marker in the title.
    if labels & NON_IMPLEMENTATION_LABELS or "[HUMAN]" in title.upper():
        return "human-gate"
    if "foreign-project" in labels:
        return "foreign-project"
    if NOT_CLAIMABLE & (labels | tags):
        return "not-claimable"
    if SENSITIVE_CATEGORY.search(title) and CATEGORY_OPT_IN not in labels:
        return "sensitive-category"
    try:
        spec_version = int(meta.get("spec_version") or 1)
    except (TypeError, ValueError):
        spec_version = 1
    if spec_version >= 2:
        criteria = " ".join(
            str(c) for c in (getattr(card, "acceptance_criteria", None) or []))
        if GATE_LANGUAGE.search(criteria):
            return "criteria-not-satisfiable"
    # Review work has its own lane with its own admission gate, and that gate
    # additionally demands a typed source binding that most review cards lack.
    # The marker is NOT only the label: `_claimability_reason` sets
    # `review_marked` from `state["review_markers"]`, which reads the title
    # too. A first version checked the label alone, promoted a batch whose
    # titles carried [REVIEW], and the selector routed every one of them to the
    # review lane where they were withheld. `ready` gained depth the fleet
    # could not use, which is the specific outcome this script must never
    # produce.
    if "review" in labels or _status(card) == "review":
        return "review-lane"
    if REVIEW_TITLE_MARKER.search(title):
        return "review-lane"
    for dep in getattr(card, "dependencies", None) or []:
        target = folded.get(str(dep))
        if target is None or not _terminal(target):
            return "dependency-open"
        if _verdict_is_blocked(home, str(dep)):
            return "dependency-blocked"
    for label in labels:
        if label.startswith("seat-") and label[len("seat-"):] not in seats:
            return "seat-unprovisioned"
    if len(SIZE_MARKER.findall(title)) != 1:
        return "size-marker"
    return None


def _load(home: Path) -> dict:
    sys.path.insert(0, str(home.parent))
    from skcoord.card_store import CardStore  # noqa: E402

    store = CardStore(str(home))
    folded = {}
    for entry in os.listdir(home / "cards"):
        try:
            card = store.fold(entry)
        except Exception:
            continue
        if card is not None:
            folded[entry] = card
    return folded


def _move(cid: str, column: str, home: Path, agent: str) -> tuple[bool, str]:
    result = subprocess.run(
        ["skcapstone", "coord", "move", cid, column, "--home", str(home),
         "--agent", agent],
        capture_output=True, text=True,
    )
    return result.returncode == 0, result.stderr.strip()[:140]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--home", default=os.path.expanduser("~/.skcapstone"))
    ap.add_argument("--target-ready", type=int, default=60)
    ap.add_argument("--max-promote", type=int, default=25)
    ap.add_argument("--agent", default="backlog-promoter")
    ap.add_argument("--apply", action="store_true",
                    help="perform the moves; without it, print the plan only")
    ap.add_argument("--reconcile", action="store_true",
                    help="also return ready cards THIS promoter placed that no "
                         "longer pass the gate, so ready never reports depth "
                         "the dispatcher cannot use")
    args = ap.parse_args()

    home = Path(args.home)
    folded = _load(home)

    try:
        seats = set(json.loads(
            (home / "coordination" / "seat-placement.json").read_text()
        ).get("seats", {}))
    except Exception as exc:
        print(f"seat placement unreadable ({exc}); refusing to act", file=sys.stderr)
        return 2

    ready, backlog = [], []
    for cid, card in folded.items():
        if _terminal(card):
            continue
        status = _status(card)
        if status == "ready":
            ready.append((cid, card))
        elif status == "backlog":
            backlog.append((cid, card))

    demoted = 0
    if args.reconcile:
        # Only ever reconsider cards this promoter placed. A card somebody else
        # readied is not ours to demote, even if our mirror of the gate refuses
        # it: our mirror can be wrong, and silently emptying another writer's
        # column would be a far worse failure than leaving one stale card.
        ours = []
        for cid, card in ready:
            reason = refusal(home, card, folded, seats)
            if reason is None:
                continue
            placed_by = str((getattr(card, "meta", None) or {}).get("last_move_agent") or "")
            events = home / "cards" / cid / "events"
            if not placed_by and events.is_dir():
                for name in os.listdir(events):
                    try:
                        with open(events / name, encoding="utf-8", errors="replace") as fh:
                            for line in fh:
                                try:
                                    row = json.loads(line)
                                except Exception:
                                    continue
                                if (isinstance(row, dict) and row.get("action") == "move"
                                        and str(row.get("agent") or "") == args.agent):
                                    placed_by = args.agent
                    except OSError:
                        continue
            if placed_by == args.agent:
                ours.append((cid, reason))
        for cid, reason in ours:
            if not args.apply:
                print(f"  WOULD RETURN {cid} to backlog ({reason})")
                continue
            ok, err = _move(cid, "backlog", home, args.agent)
            if ok:
                demoted += 1
                print(f"  RETURNED {cid} to backlog ({reason})")
            else:
                print(f"  RETURN FAILED {cid}: {err}")
        ready = [row for row in ready if row[0] not in {c for c, _ in ours}]

    shortfall = max(0, args.target_ready - len(ready))
    budget = min(shortfall, args.max_promote)
    print(f"ready={len(ready)} target={args.target_ready} backlog={len(backlog)} "
          f"shortfall={shortfall} budget={budget} demoted={demoted}")

    eligible, refused = [], {}
    for cid, card in backlog:
        reason = refusal(home, card, folded, seats)
        if reason is None:
            eligible.append((cid, card))
        else:
            refused[reason] = refused.get(reason, 0) + 1

    print("eligible=%d  refused=%s" % (
        len(eligible),
        ",".join(f"{k}={v}" for k, v in sorted(refused.items(), key=lambda x: -x[1]))
        or "none",
    ))
    if budget == 0:
        print("ready column is at target; nothing to promote")
        return 0

    eligible.sort(key=lambda row: (
        PRIORITY_RANK.get(str(getattr(row[1], "priority", "")).lower(), 9),
        str(getattr(row[1], "created_at", "")),
        row[0],
    ))
    selected = eligible[:budget]

    promoted = 0
    for cid, card in selected:
        title = str(getattr(card, "title", ""))[:66]
        if not args.apply:
            print(f"  WOULD PROMOTE {cid} | {title}")
            continue
        ok, err = _move(cid, "ready", home, args.agent)
        if ok:
            promoted += 1
            print(f"  PROMOTED {cid} | {title}")
        else:
            print(f"  FAILED   {cid} | {err}")

    if not args.apply:
        print(f"dry run: {len(selected)} would be promoted (pass --apply)")
        return 0

    # Read the column back through a FRESH fold rather than trusting exit
    # codes. A CLI that reports success and persists nothing is a documented
    # failure mode in this stack, so the count that matters is the one observed
    # afterwards, by a different path than the one that wrote it.
    sys.path.insert(0, str(home.parent))
    from skcoord.card_store import CardStore  # noqa: E402
    verify = CardStore(str(home))
    confirmed = 0
    for cid, _ in selected:
        card = verify.fold(cid)
        if card is not None and _status(card) == "ready":
            confirmed += 1
    print(f"promoted={promoted} confirmed_ready_on_reread={confirmed}")
    return 0 if confirmed == promoted else 1


if __name__ == "__main__":
    raise SystemExit(main())
