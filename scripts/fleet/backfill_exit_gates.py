#!/usr/bin/env python3
"""Split gate language out of acceptance_criteria on the measured tail.

Scope is deliberately narrow: cards still OPEN and claimed 3 or more times.
That is the tail that actually costs dispatch cycles. Closed cards are never
touched.

core.json is the only copy of acceptance_criteria, and --apply overwrites it
in place. There is no ledger to recover a pre-split state from. Before the
first card is touched, --apply writes a backup file with the pre-split
acceptance_criteria for every card in the batch, keyed by card id, and
refuses to run if that backup cannot be written.

Dry run by default. Pass --apply to write.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

# Paired with _GATE_LANGUAGE_RE in scripts/fleet/skfleet-rotate.py. Keep both
# in sync: a card split by one definition and judged unsatisfiable by the
# other reintroduces the claim-loop this script exists to cure.
GATE_LANGUAGE_RE = re.compile(
    r"independent review|review pass|before merge|approved by|sign-?off"
    r"|reviewed by|merged to main|awaiting review",
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
    """Count event actions in this card's event log.

    Also folds in a synthetic "_done_by_move" count for any move event to
    column "done". That matches skfleet-rotate.py's own completion-evidence
    semantics: its _completion_epoch treats action == "complete" and
    (action == "move" and column == "done") as equal in standing, because
    the dispatcher's own fold sets status = "done" on both. A card closed
    either way is not OPEN and must not be in scope here.
    """
    counts: collections.Counter = collections.Counter()
    events_dir = card_dir / "events"
    if not events_dir.exists():
        return counts
    for log in events_dir.glob("*.jsonl"):
        for line in log.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                event = json.loads(line)
            except Exception:
                continue
            action = event.get("action")
            counts[action] += 1
            if action == "move" and str(event.get("column") or "").strip().lower() == "done":
                counts["_done_by_move"] += 1
    return counts


Candidate = tuple[Path, dict, list[str], list[dict]]


def discover_candidates(
    home: Path, min_claims: int
) -> tuple[list[Candidate], list[str], list[str]]:
    """Walk home/cards and partition it into writable candidates and skips.

    A card qualifies only if it is still genuinely OPEN: no complete event,
    no void event, no archive event, and no move event to column "done"
    (the dispatcher treats move-to-done as equal in standing to complete;
    see card_actions). It must also be claimed at least min_claims times,
    and the split must move at least one criterion into exit_gates. A card
    whose split would leave acceptance_criteria empty is never written; its
    id is returned in the second list for a human to look at instead.

    A card whose core.json cannot be read (missing on disk mid-walk,
    permission denied, corrupt JSON, or valid JSON that is not an object)
    is skipped rather than aborting the whole walk. Its id is returned in
    the third list so the caller can report how many were skipped.
    """
    cards_dir = home / "cards"
    candidates: list[Candidate] = []
    skipped_zero_kept: list[str] = []
    unreadable: list[str] = []
    if not cards_dir.exists():
        return candidates, skipped_zero_kept, unreadable
    for card_dir in sorted(cards_dir.iterdir()):
        core_path = card_dir / "core.json"
        if not core_path.exists():
            continue
        try:
            core = json.loads(core_path.read_text(encoding="utf-8"))
            if not isinstance(core, dict):
                raise ValueError("core.json did not decode to a JSON object")
        except (OSError, ValueError):
            unreadable.append(card_dir.name)
            continue
        criteria = core.get("acceptance_criteria") or []
        if not criteria:
            continue
        counts = card_actions(card_dir)
        if counts.get("complete") or counts.get("void") or counts.get("archive"):
            continue
        if counts.get("_done_by_move"):
            continue
        if counts.get("claim", 0) < min_claims:
            continue
        kept, gates = split_criteria([str(c) for c in criteria])
        if not gates:
            continue
        if not kept:
            skipped_zero_kept.append(core.get("id") or card_dir.name)
            continue
        candidates.append((card_dir, core, kept, gates))
    return candidates, skipped_zero_kept, unreadable


def default_backup_path(home: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return home / "fleet-backups" / f"backfill_exit_gates-{stamp}.json"


def write_backup(backup_path: Path, candidates: list[Candidate]) -> None:
    """Write the pre-split acceptance_criteria for every candidate, by id.

    Raises OSError if the file cannot be written. Callers must call this,
    and it must succeed, before any card in candidates is modified.
    """
    payload = {
        (core.get("id") or card_dir.name): (core.get("acceptance_criteria") or [])
        for card_dir, core, _kept, _gates in candidates
    }
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = backup_path.with_name(backup_path.name + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, backup_path)


def atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON to path atomically: temp file in the same dir, then replace.

    Key order is preserved, not sorted, so a touched card does not show up
    as a whole-file diff across the shared store. Same-directory matters:
    os.replace is only atomic within a filesystem.
    """
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default=str(Path.home() / ".skcapstone"))
    parser.add_argument("--min-claims", type=int, default=3)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--backup-path",
        default=None,
        help="Where to write the pre-split backup. Defaults under --home.",
    )
    args = parser.parse_args(argv)

    home = Path(args.home)
    candidates, skipped_zero_kept, unreadable = discover_candidates(home, args.min_claims)

    print(f"cards in scope: {len(candidates)}")
    for card_dir, _core, kept, gates in candidates:
        print(f"  {card_dir.name}: {len(kept)} kept, {len(gates)} moved to exit_gates")

    print(
        "skipped, split would leave zero acceptance criteria: "
        f"{len(skipped_zero_kept)}"
    )
    for card_id in skipped_zero_kept:
        print(f"  {card_id}")

    print(f"unreadable core.json, skipped: {len(unreadable)}")
    for card_id in unreadable:
        print(f"  {card_id}")

    if not args.apply:
        print("dry run. pass --apply to write")
        return 0

    backup_path = (
        Path(args.backup_path) if args.backup_path else default_backup_path(home)
    )
    try:
        write_backup(backup_path, candidates)
    except OSError as exc:
        print(f"refusing to apply: backup could not be written to {backup_path}: {exc}")
        return 1
    print(f"backup of pre-split acceptance_criteria written to: {backup_path}")

    for card_dir, core, kept, gates in candidates:
        core["acceptance_criteria"] = kept
        core["exit_gates"] = gates
        core["spec_version"] = 2
        atomic_write_json(card_dir / "core.json", core)
        print(f"  split {card_dir.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
