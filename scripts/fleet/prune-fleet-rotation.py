#!/usr/bin/env python3
"""Prune old fleet rotation report directories without losing live-card evidence."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

TERMINAL_ACTIONS = frozenset({"complete", "void"})
DEFAULT_REPORT_NAMES = ("fleet-rotation", "fleet-rotation-reconciliation")


@dataclass(frozen=True)
class PruneCounts:
    """Summary counters for one pruning run."""

    scanned: int = 0
    recent: int = 0
    protected: int = 0
    eligible: int = 0
    deleted: int = 0


def parse_events(path: Path) -> list[dict[str, Any]]:
    """Parse every nonblank JSONL line from one event stream.

    The scan fails closed on malformed data so a partial CardStore view can never
    authorize deletion.
    """
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"malformed CardStore JSON at {path}:{line_number}") from exc
            if not isinstance(event, dict):
                raise ValueError(f"non-object CardStore event at {path}:{line_number}")
            events.append(event)
    return events


def iter_strings(value: Any) -> Iterable[str]:
    """Yield every string value nested in a parsed JSON value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_strings(item)


def live_card_references(cards_root: Path) -> set[str]:
    """Return strings from events belonging to non-terminal native cards."""
    references: set[str] = set()
    if not cards_root.exists():
        raise FileNotFoundError(f"CardStore does not exist: {cards_root}")
    for card_dir in sorted(path for path in cards_root.iterdir() if path.is_dir()):
        events: list[dict[str, Any]] = []
        event_dir = card_dir / "events"
        if event_dir.is_dir():
            for stream in sorted(event_dir.glob("*.jsonl")):
                events.extend(parse_events(stream))
        if any(event.get("action") in TERMINAL_ACTIONS for event in events):
            continue
        for event in events:
            references.update(iter_strings(event))
    return references


def is_referenced(report: Path, references: set[str]) -> bool:
    """Return whether live-card event text refers to this report directory."""
    report_text = str(report)
    resolved_text = str(report.resolve())
    marker = f"/{report.name}/"
    for reference in references:
        normalized = reference.rstrip("/")
        if normalized in {report_text, resolved_text, report.name}:
            return True
        if normalized.startswith(report_text + "/") or normalized.startswith(resolved_text + "/"):
            return True
        if marker in reference or reference.endswith("/" + report.name):
            return True
    return False


def prune_root(
    root: Path,
    references: set[str],
    cutoff_epoch: float,
    dry_run: bool,
) -> PruneCounts:
    """Prune eligible immediate child directories under one report root."""
    counts = {"scanned": 0, "recent": 0, "protected": 0, "eligible": 0, "deleted": 0}
    if not root.exists():
        return PruneCounts(**counts)
    if not root.is_dir():
        raise NotADirectoryError(root)
    for report in sorted(root.iterdir()):
        if not report.is_dir() or report.is_symlink():
            continue
        counts["scanned"] += 1
        if report.stat().st_mtime >= cutoff_epoch:
            counts["recent"] += 1
        elif is_referenced(report, references):
            counts["protected"] += 1
        else:
            counts["eligible"] += 1
            if not dry_run:
                shutil.rmtree(report)
                counts["deleted"] += 1
    return PruneCounts(**counts)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home() / ".skcapstone")
    parser.add_argument("--days", type=float, default=7.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--authority-host",
        default=None,
        help="refuse to run unless hostname matches, for timer host fencing",
    )
    parser.add_argument(
        "--report-name",
        action="append",
        dest="report_names",
        help="evidence child to prune, repeatable (defaults to both rotation stores)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the fail-closed safety scan and prune selected report stores."""
    args = build_parser().parse_args(argv)
    if args.days < 0:
        raise SystemExit("--days must be non-negative")
    if args.authority_host and socket.gethostname().split(".", 1)[0] != args.authority_host:
        raise SystemExit(f"refusing to run outside authority host {args.authority_host}")

    references = live_card_references(args.home / "cards")
    cutoff = time.time() - args.days * 86400
    report_names = args.report_names or DEFAULT_REPORT_NAMES
    total = PruneCounts()
    for name in report_names:
        if Path(name).name != name or name in {".", ".."}:
            raise SystemExit(f"invalid --report-name: {name}")
        counts = prune_root(args.home / "evidence" / name, references, cutoff, args.dry_run)
        print(
            f"{name}: scanned={counts.scanned} recent={counts.recent} "
            f"protected={counts.protected} eligible={counts.eligible} "
            f"deleted={counts.deleted} dry_run={str(args.dry_run).lower()}"
        )
        total = PruneCounts(
            *(
                getattr(total, field) + getattr(counts, field)
                for field in counts.__dataclass_fields__
            )
        )
    print(
        f"total: scanned={total.scanned} recent={total.recent} protected={total.protected} "
        f"eligible={total.eligible} deleted={total.deleted} dry_run={str(args.dry_run).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
