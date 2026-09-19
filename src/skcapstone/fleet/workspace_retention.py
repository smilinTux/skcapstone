"""Conservative retention for terminal skfleet worker workspaces."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skcoord.card_store import CardStore, Column

_CARD_ID = re.compile(r"(?:^|-)([0-9a-f]{8})(?:-|$)", re.IGNORECASE)
_SHA256 = re.compile(r"(?<![0-9a-f])(?:sha256:)?([0-9a-f]{64})(?![0-9a-f])", re.IGNORECASE)
_TERMINAL_ACTIONS = frozenset({"complete", "void"})


@dataclass(frozen=True)
class ArchiveResult:
    workspace: Path
    card_id: str | None
    size_bytes: int
    disposition: str
    archive: Path | None = None
    reason: str | None = None


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _events(store: CardStore, card_id: str) -> list[dict]:
    """Use CardStore's sanctioned local and legacy event union."""
    events = store._read_events(card_id) + store._legacy_events(card_id)  # noqa: SLF001
    unique: dict[tuple[object, ...], dict] = {}
    for event in events:
        key = (
            event.get("event_id"),
            event.get("ts"),
            event.get("writer"),
            event.get("seq"),
            event.get("action"),
        )
        unique[key] = event
    return sorted(
        unique.values(),
        key=lambda event: (
            event.get("ts", ""),
            event.get("writer", ""),
            event.get("seq", 0),
        ),
    )


def terminal_transition(store: CardStore, card_id: str) -> datetime | None:
    """Return the effective terminal transition time from folded lifecycle state."""
    card = store.fold(card_id)
    if card is None:
        return None

    if card.meta.get("voided") is True:
        action = "void"
    elif card.status == Column.DONE:
        action = "complete"
    else:
        return None

    transitions = [event for event in _events(store, card_id) if event.get("action") == action]
    if not transitions:
        return None
    return _parse_time(transitions[-1].get("ts"))


def _card_id(name: str) -> str | None:
    matches = _CARD_ID.findall(name)
    return matches[-1].lower() if matches else None


def _tree_size(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except FileNotFoundError:
            continue
    return total


def _referenced_hashes(home: Path) -> set[str]:
    """Extract explicit sha256 values from CardStore event JSON without trusting prose."""
    roots = (home / "cards", home / "coordination" / "card_events")
    hashes: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for line in lines:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                serialized = json.dumps(event, sort_keys=True)
                hashes.update(match.lower() for match in _SHA256.findall(serialized))
    return hashes


def _contains_evidence(path: Path, referenced: set[str]) -> bool:
    if not referenced:
        return False
    for entry in path.rglob("*"):
        if not entry.is_file() or entry.is_symlink():
            continue
        digest = hashlib.sha256()
        try:
            with entry.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            return True
        if digest.hexdigest() in referenced:
            return True
    return False


def _create_archive(workspace: Path, archive_dir: Path, card_id: str) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    destination = archive_dir / f"{workspace.name}-{card_id}.tar.zst"
    if destination.exists():
        raise FileExistsError(f"archive already exists: {destination}")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=archive_dir)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        subprocess.run(
            ["tar", "--zstd", "-cf", str(temporary), "-C", str(workspace.parent), workspace.name],
            check=True,
        )
        subprocess.run(
            ["tar", "--zstd", "-tf", str(temporary)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        os.replace(temporary, destination)
        shutil.rmtree(workspace)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def archive_workspaces(
    home: Path,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    minimum_age: timedelta = timedelta(days=7),
) -> list[ArchiveResult]:
    """Archive eligible immediate child workspaces, failing closed on uncertain state."""
    home = home.expanduser().resolve()
    workspace_root = home / "fleet" / "workspaces"
    archive_root = home / "fleet" / "workspaces-archive"
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    store = CardStore(home)
    referenced = _referenced_hashes(home)
    results: list[ArchiveResult] = []
    if not workspace_root.exists():
        return results

    for workspace in sorted(workspace_root.iterdir()):
        if not workspace.is_dir() or workspace.is_symlink():
            continue
        size = _tree_size(workspace)
        card_id = _card_id(workspace.name)
        if card_id is None:
            results.append(ArchiveResult(workspace, None, size, "skipped", reason="no card id"))
            continue
        try:
            card = store.fold(card_id)
            transition = terminal_transition(store, card_id) if card is not None else None
        except (OSError, ValueError):
            card, transition = None, None
        if card is None or transition is None:
            results.append(
                ArchiveResult(
                    workspace, card_id, size, "skipped", reason="card is live or unreadable"
                )
            )
            continue
        if transition > now - minimum_age:
            results.append(
                ArchiveResult(workspace, card_id, size, "skipped", reason="terminal under 7 days")
            )
            continue
        if _contains_evidence(workspace, referenced):
            results.append(
                ArchiveResult(
                    workspace, card_id, size, "skipped", reason="contains hashed evidence"
                )
            )
            continue
        if dry_run:
            results.append(ArchiveResult(workspace, card_id, size, "would_archive"))
            continue
        archive = _create_archive(workspace, archive_root, card_id)
        results.append(ArchiveResult(workspace, card_id, size, "archived", archive=archive))
    return results


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")
