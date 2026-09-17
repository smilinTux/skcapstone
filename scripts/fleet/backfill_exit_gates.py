#!/usr/bin/env python3
"""Split gate language out of acceptance_criteria on the measured tail.

Scope is deliberately narrow: cards still OPEN and claimed 3 or more times.
That is the tail that actually costs dispatch cycles. Closed cards are never
touched, and the event ledger is append-only, so the pre-split state remains
recoverable.

Dry run by default. Pass --apply to write.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

GATE_LANGUAGE_RE = re.compile(
    r"independent review|reviewer|review pass|before merge|approval|approved by"
    r"|sign-?off|merged",
    re.I,
)
DEFAULT_GATE_OWNER = "seraph"


def split_criteria(criteria: list[str]) -> tuple[list[str], list[dict]]:
    """Return (worker-owned criteria, derived exit gates). Nothing is dropped."""
    kept: list[str] = []
    gates: list[dict] = []
    for criterion in criteria:
        text = str(criterion)
        if GATE_LANGUAGE_RE.search(text):
            gates.append(
                {
                    "gate": "independent-review",
                    "owner": DEFAULT_GATE_OWNER,
                    "criterion": text,
                }
            )
        else:
            kept.append(text)
    return kept, gates


def card_actions(card_dir: Path) -> collections.Counter:
    counts: collections.Counter = collections.Counter()
    events_dir = card_dir / "events"
    if not events_dir.exists():
        return counts
    for log in events_dir.glob("*.jsonl"):
        for line in log.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                counts[json.loads(line).get("action")] += 1
            except Exception:
                continue
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default=str(Path.home() / ".skcapstone"))
    parser.add_argument("--min-claims", type=int, default=3)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    home = Path(args.home)
    candidates = []
    for card_dir in (home / "cards").iterdir():
        core_path = card_dir / "core.json"
        if not core_path.exists():
            continue
        core = json.loads(core_path.read_text(encoding="utf-8"))
        criteria = core.get("acceptance_criteria") or []
        if not criteria:
            continue
        counts = card_actions(card_dir)
        if counts.get("complete") or counts.get("void"):
            continue
        if counts.get("claim", 0) < args.min_claims:
            continue
        kept, gates = split_criteria([str(c) for c in criteria])
        if gates:
            candidates.append((card_dir, core, kept, gates))

    print(f"cards in scope: {len(candidates)}")
    for card_dir, _core, kept, gates in candidates:
        print(f"  {card_dir.name}: {len(kept)} kept, {len(gates)} moved to exit_gates")

    if not args.apply:
        print("dry run. pass --apply to write")
        return 0

    for card_dir, core, kept, gates in candidates:
        core["acceptance_criteria"] = kept
        core["exit_gates"] = gates
        core["spec_version"] = 2
        (card_dir / "core.json").write_text(
            json.dumps(core, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"  split {card_dir.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
