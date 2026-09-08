"""Conservative garbage collection for stale one-shot agent homes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from skcoord.card_store import CardStore, Column

WORKER_PREFIXES = ("pi-", "codex-", "pi-glm-", "pi-qwen-", "pi-codex-")
RESERVED_SEATS = frozenset({"jarvis", "lumina", "mero", "link", "human"})


@dataclass(frozen=True)
class Candidate:
    """An agent home selected for deletion."""

    name: str
    path: str
    last_touched: str
    age_days: int


def is_worker_name(name: str) -> bool:
    """Return whether a name is a non-reserved one-shot worker name."""
    normalized = name.casefold()
    if normalized in RESERVED_SEATS or "template" in normalized:
        return False
    return normalized.startswith(WORKER_PREFIXES)


def live_claim_owners(home: Path) -> set[str]:
    """Fold CardStore and return owners of claims on cards that are not done.

    Folding is deliberately strict. A malformed card aborts collection rather
    than allowing an unreadable live claim to be mistaken for an absent claim.
    """
    owners: set[str] = set()
    for card in CardStore(home).list_cards(include_archived=True):
        if card.owner and card.status != Column.DONE:
            owners.add(card.owner)
    return owners


def _tree_last_touched(path: Path) -> float:
    """Return the newest lstat timestamp in a home without following symlinks."""
    newest = path.lstat().st_mtime
    for root, directories, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        for name in directories + files:
            newest = max(newest, (root_path / name).lstat().st_mtime)
    return newest


def find_candidates(
    agents_root: Path,
    owners: set[str],
    *,
    now: datetime,
    max_age_days: int,
) -> list[Candidate]:
    """Find stale worker homes while preserving symlinks and ineligible names."""
    cutoff = now.timestamp() - timedelta(days=max_age_days).total_seconds()
    candidates: list[Candidate] = []
    for path in sorted(agents_root.iterdir(), key=lambda item: item.name):
        if path.is_symlink() or not path.is_dir() or not is_worker_name(path.name):
            continue
        if path.name in owners:
            continue
        touched = _tree_last_touched(path)
        if touched >= cutoff:
            continue
        touched_at = datetime.fromtimestamp(touched, tz=timezone.utc)
        candidates.append(
            Candidate(
                name=path.name,
                path=str(path),
                last_touched=touched_at.isoformat(),
                age_days=(now.date() - touched_at.date()).days,
            )
        )
    return candidates


def _write_report(path: Path, report: dict[str, object]) -> str:
    """Atomically write a serialized JSON report and return its SHA-256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    temporary.replace(path)
    return hashlib.sha256(payload).hexdigest()


def collect(
    home: Path,
    report_path: Path,
    *,
    apply: bool = False,
    max_age_days: int = 30,
    now: datetime | None = None,
) -> dict[str, object]:
    """Report or remove eligible homes beneath ``home/agents``.

    Evidence is outside that directory and is never traversed. Deletion is an
    explicit opt-in, while the default operation is a dry run.
    """
    if max_age_days < 1:
        raise ValueError("max_age_days must be positive")
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    agents_root = home / "agents"
    owners = live_claim_owners(home)
    candidates = find_candidates(agents_root, owners, now=current_time, max_age_days=max_age_days)

    deleted: list[str] = []
    failures: list[dict[str, str]] = []
    if apply:
        for candidate in candidates:
            path = Path(candidate.path)
            try:
                if path.parent != agents_root or path.is_symlink() or not path.is_dir():
                    raise RuntimeError("candidate changed after scan")
                shutil.rmtree(path)
                deleted.append(candidate.name)
            except Exception as exc:  # noqa: BLE001 - retain all homes on uncertain failure
                failures.append({"name": candidate.name, "error": str(exc)})

    report: dict[str, object] = {
        "schema_version": 1,
        "generated_at": current_time.isoformat(),
        "host": socket.gethostname(),
        "mode": "apply" if apply else "dry-run",
        "home": str(home),
        "agents_root": str(agents_root),
        "evidence_root_untouched": str(home / "evidence" / "work"),
        "max_age_days": max_age_days,
        "live_claim_owners": sorted(owners),
        "candidates": [asdict(candidate) for candidate in candidates],
        "deleted": deleted,
        "failures": failures,
    }
    digest = _write_report(report_path, report)
    report["report_path"] = str(report_path)
    report["report_sha256"] = digest
    return report


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--home",
        type=Path,
        default=Path(os.environ.get("SKCAPSTONE_HOME", "~/.skcapstone")).expanduser(),
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete reported candidates; without this flag only write a dry-run report",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run agent-home collection and print the report identity."""
    args = _parser().parse_args(argv)
    try:
        report = collect(
            args.home.expanduser(),
            args.report.expanduser(),
            apply=args.apply,
            max_age_days=args.max_age_days,
        )
    except Exception as exc:  # noqa: BLE001 - CLI must fail closed with a useful reason
        print(f"agent-home-gc: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
