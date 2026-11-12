"""Governed cleanup for coordination noise: legacy reaper rows and conflict copies.

Two operator commands, both dry-run by default and both non-destructive:

``legacy-verdicts``
    The liveness and wedge reapers used to append
    ``{"action":"verdict","verdict":"WORKER_DIED",...}`` rows to
    ``coordination/card_events/<writer>.jsonl``. ``CardEvent`` forbids the
    ``verdict`` field, so the fold rejected every such row. This command moves
    exactly those rows, byte for byte, into a dated archive under
    ``coordination/recovery/legacy-reaper-verdicts/`` and rewrites the live file
    with every other line untouched. A row is moved only if it is currently
    rejected by the overlay validator, so the fold loses nothing it uses. The
    ``worker_died`` / ``worker_wedged`` link row written with each verdict
    stays and is what the fold reads.

``quarantine-conflicts``
    Moves Syncthing ``*.sync-conflict-*`` copies out of the directories the
    fold reads into ``coordination/recovery/sync-conflict-bulk/<YYYYMMDD>/``,
    recording a sha256 manifest. Nothing is deleted. Append-only stores
    (``card_events``, ``archive``) can hold lines that exist nowhere else, so
    they are skipped unless ``--include-append-only`` is passed.

Run: ``python -m skcapstone.coord_hygiene <command> [--home DIR] [--apply]``.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from skcoord.card import CardEvent, validate_overlay_event

#: Reaper writers whose pre-fix rows carried the forbidden ``verdict`` field.
LEGACY_VERDICT_WRITERS = ("fleet-liveness-reaper", "fleet-wedge-reaper")

SYNC_CONFLICT_RE = re.compile(r"\.sync-conflict-\d{8}-\d{6}-[A-Za-z0-9]+(?:\.|$)")

#: Directories under coordination/ that hold one record per file.
RECORD_DIRS = ("agents", "tasks")
#: Append-only stores; a conflict copy may hold unique lines.
APPEND_ONLY_DIRS = ("card_events", "archive")


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%d")


def _fsync_write(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unique(path: Path) -> Path:
    candidate, n = path, 0
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.name}.{n}")
    return candidate


def _append_manifest(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, (json.dumps(record, sort_keys=True) + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rejected_by_fold(line: bytes) -> bool:
    """True when the overlay reader would drop this line as not a card event."""
    try:
        validate_overlay_event(CardEvent.model_validate_json(line))
    except Exception:  # noqa: BLE001 - mirrors CardEventLog.read_all
        return True
    return False


def is_legacy_verdict_row(line: bytes) -> bool:
    """True for a reaper verdict row in the old shape that the fold rejects."""
    try:
        row = json.loads(line)
    except ValueError:
        return False
    return (
        isinstance(row, dict)
        and row.get("action") == "verdict"
        and "verdict" in row
        and "link_key" not in row
        and _rejected_by_fold(line)
    )


@dataclass
class LegacyVerdictReport:
    """Outcome of one ``legacy-verdicts`` run."""

    moved: dict[str, int] = field(default_factory=dict)
    archive: dict[str, str] = field(default_factory=dict)
    applied: bool = False


def archive_legacy_verdicts(
    home: Path, *, apply: bool = False, now: datetime | None = None
) -> LegacyVerdictReport:
    """Move old-shape reaper verdict rows out of the live overlay, byte for byte."""
    home = Path(home)
    events = home / "coordination" / "card_events"
    out = home / "coordination" / "recovery" / "legacy-reaper-verdicts" / _stamp(now)
    report = LegacyVerdictReport(applied=apply)
    for writer in LEGACY_VERDICT_WRITERS:
        live = events / f"{writer}.jsonl"
        if not live.is_file() or live.is_symlink():
            continue
        with open(live, "r+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # same lock the reapers append under
            try:
                original = handle.read()
                lines = original.splitlines(keepends=True)
                moved = [ln for ln in lines if is_legacy_verdict_row(ln.rstrip(b"\r\n"))]
                if not moved:
                    continue
                report.moved[writer] = len(moved)
                if not apply:
                    continue
                out.mkdir(parents=True, exist_ok=True)
                moved_path = _unique(out / f"{writer}.verdict-rows.jsonl")
                backup_path = _unique(out / f"{writer}.pre-migration.jsonl")
                # Full copy first, so the rewrite below can never be the only copy.
                _fsync_write(backup_path, original)
                _fsync_write(moved_path, b"".join(moved))
                kept = b"".join(ln for ln in lines if ln not in moved)
                handle.seek(0)
                handle.truncate()
                handle.write(kept)
                handle.flush()
                os.fsync(handle.fileno())
                report.archive[writer] = str(moved_path)
                _append_manifest(
                    out / "manifest.jsonl",
                    {
                        "writer": writer,
                        "moved_rows": len(moved),
                        "moved_sha256": hashlib.sha256(b"".join(moved)).hexdigest(),
                        "original_sha256": hashlib.sha256(original).hexdigest(),
                        "kept_sha256": hashlib.sha256(kept).hexdigest(),
                        "moved_file": str(moved_path),
                        "backup_file": str(backup_path),
                        "at": (now or datetime.now(timezone.utc)).isoformat(),
                    },
                )
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return report


@dataclass
class ConflictReport:
    """Outcome of one ``quarantine-conflicts`` run."""

    found: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)
    applied: bool = False


def quarantine_conflicts(
    home: Path,
    *,
    apply: bool = False,
    include_append_only: bool = False,
    now: datetime | None = None,
) -> ConflictReport:
    """Move Syncthing conflict copies out of the directories the fold reads."""
    home = Path(home)
    coordination = home / "coordination"
    dest_root = coordination / "recovery" / "sync-conflict-bulk" / _stamp(now)
    names = RECORD_DIRS + (APPEND_ONLY_DIRS if include_append_only else ())
    report = ConflictReport(applied=apply)
    for name in names:
        directory = coordination / name
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in sorted(directory.iterdir()):
            if not SYNC_CONFLICT_RE.search(path.name):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            report.found.append(str(path.relative_to(coordination)))
            if not apply:
                continue
            target = _unique(dest_root / name / path.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            os.rename(path, target)  # same filesystem, never replaces (target is unique)
            report.moved.append(str(target))
            _append_manifest(
                dest_root / "manifest.jsonl",
                {
                    "source": str(path.relative_to(coordination)),
                    "quarantined_to": str(target),
                    "sha256": digest,
                    "at": (now or datetime.now(timezone.utc)).isoformat(),
                },
            )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skcapstone.coord_hygiene", description=__doc__)
    parser.add_argument("command", choices=("legacy-verdicts", "quarantine-conflicts"))
    parser.add_argument("--home", type=Path, default=Path.home() / ".skcapstone")
    parser.add_argument("--apply", action="store_true", help="make the change (default: dry run)")
    parser.add_argument(
        "--include-append-only",
        action="store_true",
        help="quarantine-conflicts: also card_events and archive (may hold unique lines)",
    )
    args = parser.parse_args(argv)
    mode = "APPLY" if args.apply else "DRY-RUN"
    if args.command == "legacy-verdicts":
        report = archive_legacy_verdicts(args.home, apply=args.apply)
        total = sum(report.moved.values())
        print(f"{mode} legacy-verdicts: {total} row(s) {report.moved}")
        for writer, path in report.archive.items():
            print(f"  archived {writer} -> {path}")
    else:
        report = quarantine_conflicts(
            args.home, apply=args.apply, include_append_only=args.include_append_only
        )
        print(f"{mode} quarantine-conflicts: {len(report.found)} file(s)")
        for item in report.found:
            print(f"  {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
