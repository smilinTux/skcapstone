#!/usr/bin/env python3
"""Report or explicitly label cards whose exact card blockers are DONE."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from skcapstone.blocker_referent import (  # noqa: E402
    apply_candidates,
    find_returnable,
    find_stale_blocks,
)


def main(argv: list[str] | None = None) -> int:
    """Run the read-only report or the explicit, locked label application."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go", action="store_true", help="label qualifying cards")
    parser.add_argument("--agent", default=os.environ.get("SKAGENT", "lumina"))
    parser.add_argument("--home", default=os.path.expanduser("~/.skcapstone"))
    args = parser.parse_args(argv)
    home = Path(args.home).expanduser()

    try:
        report = find_returnable(home)
        stale = find_stale_blocks(home)
    except Exception as exc:  # noqa: BLE001 - report the board read failure
        print(f"blocker-referent sweep failed closed: {exc}", file=sys.stderr)
        return 2

    print("blocker-referent sweep")
    print(f"  returnable: {len(report.candidates)}")
    print(f"  held or invalid: {len(report.held)}")
    for card_id, reason in sorted(report.held.items()):
        print(f"    held {card_id}: {reason}")
    for candidate in report.candidates:
        refs = ",".join(candidate.referents)
        print(
            f"    candidate {candidate.card_id}: verdict={candidate.verdict.identity} "
            f"referents={refs}"
        )

    if stale:
        # Blocks whose own named repair has already passed. Reported, never
        # cleared: whether a successor genuinely discharges its parent is a
        # judgement about evidence, not a timestamp comparison. Closed cards are
        # included on purpose, because a stale verdict does its damage through
        # whoever reads it rather than through the card's own column.
        print(f"  blocks answered by their own successor: {len(stale)}")
        for row in stale:
            print(
                f"    {row.card_id} blocked {row.verdict.ts[:16]}, "
                f"{row.link} {row.successor} passed {row.passed_at[:16]}"
            )

    if not args.go:
        print("  report only; no labels written")
        return 0
    if not report.candidates:
        print("  labelled 0 of 0")
        return 0

    receipts = apply_candidates(
        home,
        report.candidates,
        agent=args.agent,
    )
    for receipt in receipts:
        stream = sys.stderr if receipt.state == "failed" else sys.stdout
        print(
            f"    {receipt.state} {receipt.card_id}: "
            f"verdict={receipt.verdict_id} detail={receipt.detail}",
            file=stream,
        )
    labelled = sum(receipt.state == "labelled" for receipt in receipts)
    failed = sum(receipt.state == "failed" for receipt in receipts)
    print(f"  labelled {labelled} of {len(receipts)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
