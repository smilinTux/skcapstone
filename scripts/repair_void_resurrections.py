#!/usr/bin/env python3
"""Re-archive cards resurrected after a recorded void decision.

This repair never rewrites a void event. It identifies cards whose latest
structural event after a void is ``move`` or ``reopen``, appends a fresh archive
through the dual-projection Board API, and verifies both projections plus the
original void bytes before reporting success.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from skcoord.card_store import CardStore

from skcapstone.coordination import Board

STRUCTURAL = {"complete", "move", "archive", "reopen", "void"}
RESURRECTING = {"move", "reopen"}


def resurrected_voids(store: CardStore) -> list[tuple[str, dict]]:
    """Return voided cards whose latest structural event made them live."""
    found: list[tuple[str, dict]] = []
    for card_id in store.list_card_ids():
        events = store._read_events(card_id)
        voids = [event for event in events if event.get("action") == "void"]
        structural = [event for event in events if event.get("action") in STRUCTURAL]
        if voids and structural and structural[-1].get("action") in RESURRECTING:
            found.append((card_id, dict(voids[-1])))
    return found


def repair(home: Path, *, writer: str, apply: bool) -> dict:
    """Append and verify archive events, or report the exact dry-run set."""
    store = CardStore(home)
    board = Board(home)
    candidates = resurrected_voids(store)
    repaired: list[str] = []
    for card_id, original_void in candidates:
        if not apply:
            repaired.append(card_id)
            continue
        board.archive_task(card_id, by=writer)
        current_voids = [
            event for event in store._read_events(card_id) if event.get("action") == "void"
        ]
        folded = store.fold(card_id)
        if not current_voids or current_voids[-1] != original_void:
            raise RuntimeError(f"void audit record changed while repairing {card_id}")
        if card_id not in board.archived_ids() or folded is None or not folded.archived:
            raise RuntimeError(f"archive did not persist in every projection for {card_id}")
        repaired.append(card_id)
    remaining = len(resurrected_voids(CardStore(home))) if apply else len(candidates)
    if apply and remaining:
        raise RuntimeError(f"{remaining} resurrected void cards remain after repair")
    return {
        "apply": apply,
        "candidate_count": len(candidates),
        "repaired_count": len(repaired) if apply else 0,
        "remaining_count": remaining,
        "card_ids": repaired,
    }


def main() -> int:
    """Run the bounded append-only reconciliation."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=Path.home() / ".skcapstone")
    parser.add_argument("--writer", default="void-reconcile")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = repair(args.home.expanduser(), writer=args.writer, apply=args.apply)
    encoded = json.dumps(result, indent=2, sort_keys=True)
    json.loads(encoded)
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
